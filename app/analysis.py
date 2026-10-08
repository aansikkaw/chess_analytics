"""Turn games + engine evaluations into per-move judgments.

The maths follows Lichess's published approach:
  * centipawns -> win probability (a logistic curve), so a 200cp swing at +0.0
    counts for much more than the same swing at +8.0 (where you're winning anyway);
  * per-move accuracy from the win-probability lost on that move;
  * inaccuracy / mistake / blunder at 10 / 20 / 30 win-% points lost.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from collections.abc import Callable

import chess

from .engine import Engine, PositionEval
from .games import GameRecord

PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

INACCURACY, MISTAKE, BLUNDER = 10.0, 20.0, 30.0  # win-% points lost
WINNING_CP = 200  # "clearly better" threshold, mover's POV
CONVERSION_PEAK_CP = 300  # a game counts as "had it won" above this
EVAL_CLAMP_CP = 1000


def win_percent(cp: int) -> float:
    """Expected score (0-100) for the side whose POV `cp` is in."""
    cp = max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(win_before: float, win_after: float) -> float:
    """Lichess move-accuracy formula: 100 for a move that loses nothing."""
    loss = max(0.0, win_before - win_after)
    acc = 103.1668 * math.exp(-0.04354 * loss) - 3.1669
    return max(0.0, min(100.0, acc))


def classify(win_loss: float) -> str:
    if win_loss >= BLUNDER:
        return "blunder"
    if win_loss >= MISTAKE:
        return "mistake"
    if win_loss >= INACCURACY:
        return "inaccuracy"
    return "good"


def game_phase(board: chess.Board) -> str:
    """Opening / middlegame / endgame from move number and non-pawn material."""
    non_pawn = sum(
        PIECE_VALUE[p.piece_type]
        for p in board.piece_map().values()
        if p.piece_type not in (chess.PAWN, chess.KING)
    )
    queens_off = not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK)
    if non_pawn <= 20 or (queens_off and non_pawn <= 26):
        return "endgame"
    if board.fullmove_number <= 12:
        return "opening"
    return "middlegame"


def is_forcing(board: chess.Board, uci: str | None) -> bool:
    """Capture, check or promotion: our proxy for a 'tactical' best move."""
    if not uci:
        return False
    move = chess.Move.from_uci(uci)
    return board.is_capture(move) or board.gives_check(move) or move.promotion is not None


def _is_tactical(board: chess.Board, best_uci: str | None) -> bool:
    """A position where the best move is forcing, excluding plain recaptures
    (taking back on the square the opponent just captured on is not a tactic)."""
    if not is_forcing(board, best_uci):
        return False
    if board.move_stack:
        last = board.peek()
        best = chess.Move.from_uci(best_uci)
        board_before_last = board.copy()
        board_before_last.pop()
        if board_before_last.is_capture(last) and best.to_square == last.to_square and not board.gives_check(best):
            return False
    return True


def _pv_san(board: chess.Board, pv: tuple[str, ...], limit: int = 5) -> list[str]:
    b = board.copy(stack=False)
    out = []
    for uci in pv[:limit]:
        move = chess.Move.from_uci(uci)
        if move not in b.legal_moves:
            break
        out.append(b.san(move))
        b.push(move)
    return out


@dataclass
class MoveAnalysis:
    game_id: str
    ply: int  # 0-based half-move index
    move_number: int
    color: str  # "white" / "black"
    fen_before: str
    played: str
    played_san: str
    best: str | None
    best_san: str | None
    best_line: list[str]
    cp_before: int  # mover's POV
    cp_after: int  # mover's POV
    win_loss: float
    accuracy: float
    classification: str
    phase: str
    clock_before: float | None
    time_pressure: bool
    tactical: bool
    tags: list[str] = field(default_factory=list)


@dataclass
class GameAnalysis:
    game_id: str
    opponent: str
    player_color: str
    player_rating: int | None
    result: str
    score: float
    time_control: str
    opening: str
    played_at: str
    accuracy: float
    peak_cp: int  # best eval the player reached, their POV
    conversion_failure: bool
    moves: list[MoveAnalysis]
    # Added in v0.3; defaults keep games saved by older versions loadable.
    moves_san: list[str] = field(default_factory=list)  # every move of the game, both sides
    time_class: str = ""
    termination: str = ""
    url: str = ""
    lost_on_time: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _time_pressure(clock: float | None, initial: int | None) -> bool:
    if clock is None:
        return False
    threshold = max(30.0, 0.1 * initial) if initial else 30.0
    return clock < threshold


def _tags(m: MoveAnalysis, before: PositionEval, after: PositionEval, board_after: chess.Board, mover: chess.Color) -> list[str]:
    """Explain *what kind* of mistake a mistake/blunder was."""
    tags: list[str] = []
    mate_before = before.mate_white if mover == chess.WHITE else (-before.mate_white if before.mate_white is not None else None)
    mate_after = after.mate_white if mover == chess.WHITE else (-after.mate_white if after.mate_white is not None else None)
    if mate_before is not None and mate_before > 0 and not (mate_after is not None and mate_after > 0):
        tags.append("missed_mate")
    if after.best_move:
        reply = chess.Move.from_uci(after.best_move)
        if board_after.is_capture(reply):
            victim = board_after.piece_at(reply.to_square)
            if victim and PIECE_VALUE[victim.piece_type] >= 3:
                tags.append("hanging_piece")
    if m.tactical and m.played != m.best:
        tags.append("missed_tactic")
    if m.time_pressure:
        tags.append("time_trouble")
    if m.cp_before >= WINNING_CP:
        tags.append("conversion")
    return tags


def analyse_game(game: GameRecord, engine: Engine) -> GameAnalysis:
    board = chess.Board()
    # evals[i] = evaluation of the position after i plies.
    evals: list[PositionEval] = [engine.evaluate(board)]
    boards: list[chess.Board] = [board.copy()]
    for uci in game.moves:
        board.push(chess.Move.from_uci(uci))
        evals.append(engine.evaluate(board))
        boards.append(board.copy())

    mover_color = game.player_color
    moves_san = [boards[i].san(chess.Move.from_uci(u)) for i, u in enumerate(game.moves)]
    analysed: list[MoveAnalysis] = []
    peak = -EVAL_CLAMP_CP
    for ply, uci in enumerate(game.moves):
        pos = boards[ply]
        if pos.turn != mover_color:
            continue
        before, after = evals[ply], evals[ply + 1]
        cp_before, cp_after = before.cp_for(mover_color), after.cp_for(mover_color)
        peak = max(peak, cp_before)
        w_before, w_after = win_percent(cp_before), win_percent(cp_after)
        loss = max(0.0, w_before - w_after)
        move = chess.Move.from_uci(uci)
        best_san = pos.san(chess.Move.from_uci(before.best_move)) if before.best_move else None
        # Our clock *before* this move = what we had left after our previous move.
        clock_before = game.clocks[ply - 2] if ply >= 2 and ply - 2 < len(game.clocks) else (
            float(game.initial_seconds) if game.initial_seconds else None
        )
        m = MoveAnalysis(
            game_id=game.game_id,
            ply=ply,
            move_number=pos.fullmove_number,
            color="white" if mover_color == chess.WHITE else "black",
            fen_before=pos.fen(),
            played=uci,
            played_san=pos.san(move),
            best=before.best_move,
            best_san=best_san,
            best_line=_pv_san(pos, before.pv),
            cp_before=cp_before,
            cp_after=cp_after,
            win_loss=round(loss, 2),
            accuracy=round(move_accuracy(w_before, w_after), 2),
            classification=classify(loss),
            phase=game_phase(pos),
            clock_before=clock_before,
            time_pressure=_time_pressure(clock_before, game.initial_seconds),
            tactical=_is_tactical(pos, before.best_move),
        )
        if m.classification in ("mistake", "blunder"):
            m.tags = _tags(m, before, after, boards[ply + 1], mover_color)
        analysed.append(m)

    accuracy = sum(m.accuracy for m in analysed) / len(analysed) if analysed else 0.0
    return GameAnalysis(
        game_id=game.game_id,
        opponent=game.black if mover_color == chess.WHITE else game.white,
        player_color="white" if mover_color == chess.WHITE else "black",
        player_rating=game.player_rating,
        result=game.result,
        score=game.player_score,
        time_control=game.time_control,
        opening=game.opening,
        played_at=game.played_at,
        accuracy=round(accuracy, 2),
        peak_cp=peak,
        conversion_failure=peak >= CONVERSION_PEAK_CP and game.player_score < 1.0,
        moves=analysed,
        moves_san=moves_san,
        time_class=game.time_class,
        termination=game.termination,
        url=game.url,
        lost_on_time=game.lost_on_time,
    )


def analyse_games(
    games: list[GameRecord],
    engine: Engine,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[GameAnalysis]:
    out = []
    for i, g in enumerate(games, 1):
        out.append(analyse_game(g, engine))
        if on_progress:
            on_progress(i, len(games))
    return out
