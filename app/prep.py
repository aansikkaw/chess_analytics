"""Prep Check (Pro): compare a ChessBase repertoire with the games actually played.

Upload a repertoire exported from ChessBase as PGN: one or more "games" whose moves and
*variations* are your preparation. The tree is keyed by position (EPD), so transpositions
are recognised. It is then compared with the player's analysed games:

  * deviation  - a position where your prep says one thing and you played another
                 (you forgot or ignored your own preparation);
  * gap        - your opponent played a move your prep doesn't cover (with an engine
                 suggestion for what to add);
  * hole       - a prepared line whose final position the engine thinks is bad for you;
  * drills     - "what does your prep say here?" positions for the spaced-repetition deck,
                 starting with the ones you've actually got wrong in games.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass, field

import chess
import chess.pgn

from .analysis import GameAnalysis
from .engine import Engine
from .games import GameImportError
from .repertoire import fmt_line

MAX_NODES = 60_000
HOLE_THRESHOLD_CP = -50  # prep line ends at -0.5 or worse for you
MAX_HOLE_CHECKS = 150
MAX_GAP_SUGGESTIONS = 12
MAX_DRILLS = 40


def epd(board: chess.Board) -> str:
    return board.epd()


@dataclass
class PrepTree:
    color: chess.Color
    my_moves: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # my turn -> prepared SAN moves
    their_moves: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # their turn -> covered replies
    fens: dict[str, str] = field(default_factory=dict)  # epd -> a full FEN for the position
    lines: dict[str, list[str]] = field(default_factory=dict)  # epd -> shortest line reaching it
    leaves: list[tuple[str, list[str]]] = field(default_factory=list)  # (fen, line) where a prepared line ends
    chapters: int = 0

    @property
    def positions(self) -> int:
        return len(self.my_moves) + len(self.their_moves)


def detect_color(pgn_text: str) -> chess.Color:
    """A repertoire branches on the *opponent's* moves; your own moves are mostly single.

    So the side with fewer branching positions is the repertoire owner.
    """
    tree_w = parse_repertoire(pgn_text, chess.WHITE)
    white_branch = sum(len(v) > 1 for v in tree_w.my_moves.values())  # white-to-move positions with options
    black_branch = sum(len(v) > 1 for v in tree_w.their_moves.values())
    return chess.WHITE if white_branch <= black_branch else chess.BLACK


def parse_repertoire(pgn_text: str, color: chess.Color) -> PrepTree:
    tree = PrepTree(color)
    stream = io.StringIO(pgn_text)
    nodes = 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        if "FEN" in game.headers and game.headers.get("SetUp") != "0":
            continue  # chapters starting from a custom position are skipped
        tree.chapters += 1
        stack = [(game, chess.Board(), [])]
        while stack:
            node, board, line = stack.pop()
            nodes += 1
            if nodes > MAX_NODES:
                raise GameImportError(f"That repertoire has more than {MAX_NODES:,} moves. Upload one colour or chapter at a time.")
            key = epd(board)
            tree.fens.setdefault(key, board.fen())
            if key not in tree.lines or len(line) < len(tree.lines[key]):
                tree.lines[key] = line
            if not node.variations:
                tree.leaves.append((board.fen(), line))
                continue
            for child in node.variations:
                mv = child.move
                if mv not in board.legal_moves:
                    continue
                san = board.san(mv)
                bucket = tree.my_moves if board.turn == color else tree.their_moves
                if san not in bucket[key]:
                    bucket[key].append(san)  # first listed = main line
                nb = board.copy(stack=False)
                nb.push(mv)
                stack.append((child, nb, line + [san]))
    if tree.positions == 0:
        raise GameImportError("No prepared moves found. Export your repertoire from ChessBase as PGN (with variations).")
    return tree


def _line_label(line: list[str]) -> str:
    return fmt_line(line) if line else "the start"


def check_repertoire(tree: PrepTree, games: list[GameAnalysis], engine: Engine | None, name: str) -> dict:
    color_name = "white" if tree.color == chess.WHITE else "black"
    mine = [g for g in games if g.player_color == color_name and g.moves_san]
    deviations: dict[tuple, dict] = {}
    gaps: dict[tuple, dict] = {}
    reached: dict[str, int] = defaultdict(int)  # my-turn prep positions reached in games
    statuses = defaultdict(int)
    in_prep_counts: list[int] = []

    for g in mine:
        board = chess.Board()
        line: list[str] = []
        my_in_prep = 0
        status = "followed_to_end"
        analysed = {m.ply: m for m in g.moves}
        for ply, san in enumerate(g.moves_san):
            key = epd(board)
            if board.turn == tree.color:
                prepared = tree.my_moves.get(key)
                if not prepared:
                    status = "prep_ended"
                    break
                reached[key] += 1
                if san not in prepared:
                    if ply == 0:
                        status = "other_opening"  # a different first move: a different repertoire
                        break
                    status = "deviated"
                    m = analysed.get(ply)
                    d = deviations.setdefault((key, san), {
                        "line": _line_label(line), "prep_moves": prepared, "played": san, "times": 0, "scores": [],
                        "fen": board.fen(), "epd": key, "examples": [], "win_loss": [],
                    })
                    d["times"] += 1
                    d["scores"].append(g.score)
                    if m:
                        d["win_loss"].append(m.win_loss)
                        if m.classification in ("mistake", "blunder"):
                            d["examples"].append({"game_id": g.game_id, "ply": ply})
                    break
                my_in_prep += 1
            else:
                covered = tree.their_moves.get(key)
                if not covered:
                    status = "prep_ended"
                    break
                if san not in covered:
                    status = "opponent_left"
                    after = board.copy(stack=False)
                    after.push_san(san)
                    gp = gaps.setdefault((key, san), {
                        "line": _line_label(line + [san]), "opponent_move": san, "covered": covered, "times": 0,
                        "scores": [], "fen_after": after.fen(),
                    })
                    gp["times"] += 1
                    gp["scores"].append(g.score)
                    break
            board.push_san(san)
            line.append(san)
        statuses[status] += 1
        if status != "other_opening":
            in_prep_counts.append(my_in_prep)

    # Engine: suggest replies for the most frequent gaps, and find prep lines that end badly.
    gap_list = sorted(gaps.values(), key=lambda x: -x["times"])
    for gp in gap_list[:MAX_GAP_SUGGESTIONS]:
        if engine is None:
            break
        b = chess.Board(gp["fen_after"])
        ev = engine.evaluate(b)
        if ev.best_move:
            gp["engine_reply"] = b.san(chess.Move.from_uci(ev.best_move))
            gp["engine_eval"] = _pawns(ev.cp_for(tree.color))
    holes = []
    if engine is not None:
        seen = set()
        for fen, line in sorted(tree.leaves, key=lambda x: len(x[1]))[:MAX_HOLE_CHECKS]:
            b = chess.Board(fen)
            if b.epd() in seen or b.is_game_over():
                continue
            seen.add(b.epd())
            cp = engine.evaluate(b).cp_for(tree.color)
            if cp <= HOLE_THRESHOLD_CP:
                holes.append({"line": _line_label(line), "eval_for_you": _pawns(cp), "cp": cp, "fen": fen})
        holes.sort(key=lambda h: h["cp"])

    dev_list = []
    for d in sorted(deviations.values(), key=lambda x: (-x["times"], -sum(x["win_loss"]))):
        dev_list.append({
            "line": d["line"], "prep_moves": d["prep_moves"], "played": d["played"], "times": d["times"],
            "score_pct": round(100 * sum(d["scores"]) / len(d["scores"])),
            "avg_winning_chances_lost": round(sum(d["win_loss"]) / len(d["win_loss"]), 1) if d["win_loss"] else None,
            "fen": d["fen"], "examples": d["examples"][:3],
        })
    checked = len(in_prep_counts)
    followed = checked - statuses["deviated"]
    report = {
        "name": name,
        "color": color_name,
        "summary": {
            "games": checked,
            "other_openings": statuses["other_opening"],
            "followed_pct": round(100 * followed / checked) if checked else 0,
            "avg_moves_in_prep": round(sum(in_prep_counts) / checked, 1) if checked else 0,
            "statuses": dict(statuses),
            "positions": tree.positions,
            "chapters": tree.chapters,
            "leaves": len(tree.leaves),
        },
        "deviations": dev_list[:20],
        "gaps": [{k: v for k, v in gp.items() if k != "scores"} | {"score_pct": round(100 * sum(gp["scores"]) / len(gp["scores"]))}
                 for gp in gap_list[:20]],
        "holes": holes[:15],
        "drills": drills(tree, deviations, reached),
    }
    return report


def drills(tree: PrepTree, deviations: dict, reached: dict) -> list[dict]:
    """Positions to drill, most useful first: ones you got wrong, then ones you reach, then the rest (shallow first)."""
    order: list[str] = []
    wrong = {}
    for (key, played), _d in sorted(deviations.items(), key=lambda kv: -kv[1]["times"]):
        if key not in wrong:
            wrong[key] = played
            order.append(key)
    order += [k for k, _ in sorted(reached.items(), key=lambda kv: -kv[1]) if k not in wrong]
    rest = sorted((k for k in tree.my_moves if k not in wrong and k not in reached), key=lambda k: len(tree.lines.get(k, [])))
    order += rest
    out = []
    for key in order[:MAX_DRILLS]:
        board = chess.Board(tree.fens[key])
        moves = tree.my_moves[key]
        try:
            accept = [board.parse_san(s).uci() for s in moves]
        except ValueError:
            continue
        out.append({
            "fen": tree.fens[key], "prep_moves": moves, "accept": accept, "line": _line_label(tree.lines.get(key, [])),
            "you_played": wrong.get(key), "reached": reached.get(key, 0),
        })
    return out


def _pawns(cp: int) -> str:
    if abs(cp) >= 9000:
        return "mate" if cp > 0 else "getting mated"
    return f"{cp / 100:+.1f}"
