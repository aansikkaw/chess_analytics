"""Thin wrapper around a Stockfish process (via python-chess's UCI support).

Scores are always returned from White's point of view in centipawns, with
forced mates mapped to +/-MATE_CP so the rest of the code only deals with numbers.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass

import chess
import chess.engine

MATE_CP = 10_000


class EngineUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionEval:
    cp_white: int  # centipawns, White's POV, mates clamped to +/-MATE_CP
    mate_white: int | None  # moves to mate, White's POV (negative = Black mates)
    best_move: str | None  # UCI
    pv: tuple[str, ...]  # principal variation, UCI

    def cp_for(self, color: chess.Color) -> int:
        return self.cp_white if color == chess.WHITE else -self.cp_white


class Engine:
    """One Stockfish process. Thread-safe: calls are serialized with a lock."""

    MAX_CACHE = 50_000

    def __init__(self, path: str | None, depth: int = 12, time_limit: float = 0.08):
        if not path:
            raise EngineUnavailable(
                "Stockfish not found. Install it (e.g. `brew install stockfish`) "
                "or set STOCKFISH_PATH."
            )
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(path)
        except (FileNotFoundError, PermissionError, chess.engine.EngineError) as exc:
            raise EngineUnavailable(f"Could not start Stockfish at {path}: {exc}") from exc
        self._limit = chess.engine.Limit(depth=depth, time=time_limit)
        self._lock = threading.Lock()
        self._cache: dict[str, PositionEval] = {}

    def evaluate(self, board: chess.Board, limit: chess.engine.Limit | None = None) -> PositionEval:
        # Key on the position only (no move counters) so transpositions share work.
        key = board.epd()
        if limit is None and key in self._cache:
            return self._cache[key]

        if board.is_game_over():
            result = self._terminal_eval(board)
        else:
            with self._lock:
                info = self._engine.analyse(board, limit or self._limit)
            score = info["score"].white()
            pv = tuple(m.uci() for m in info.get("pv", []))
            result = PositionEval(
                cp_white=score.score(mate_score=MATE_CP),
                mate_white=score.mate(),
                best_move=pv[0] if pv else None,
                pv=pv,
            )
        if limit is None:
            if len(self._cache) >= self.MAX_CACHE:
                self._cache.clear()  # crude but bounded; positions repeat mostly within one game
            self._cache[key] = result
        return result

    @staticmethod
    def _terminal_eval(board: chess.Board) -> PositionEval:
        if board.is_checkmate():
            # Side to move is mated.
            cp = -MATE_CP if board.turn == chess.WHITE else MATE_CP
            return PositionEval(cp, 0, None, ())
        return PositionEval(0, None, None, ())  # stalemate / insufficient material / etc.

    def close(self) -> None:
        with contextlib.suppress(chess.engine.EngineTerminatedError):
            self._engine.quit()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
