"""Opening names (ECO codes) for any game, from Lichess's open opening list (CC0, app/data/openings.tsv).

Scanned scoresheets and pasted games carry no opening name, so we work it out: every named line is turned
into its final position once (by position, so any move order finds it), and a game's opening is the last
named position it passes through in the first 40 plies. Every position along a named line counts as known
theory (1.d4 Nf6 2.c4 e6 has no name of its own, but it's on the way to the Catalan), which tells us when the
game "left theory".
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import chess

DATA = Path(__file__).resolve().parent / "data" / "openings.tsv"
MAX_PLIES = 40
_lock = threading.Lock()
_index: dict[str, tuple[str, str, int]] | None = None
_theory: set[str] = set()


def _key(board: chess.Board) -> str:
    return board.epd()  # pieces, side to move, castling, en passant: move counters don't matter


def index() -> dict[str, tuple[str, str, int]]:
    """position -> (eco, name, plies of the named line). Built once, on first use (about a second)."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        out: dict[str, tuple[str, str, int]] = {}
        theory: set[str] = set()
        if DATA.exists():
            with DATA.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    board = chess.Board()
                    seen = []
                    try:
                        for tok in row["pgn"].split():
                            if tok[0].isdigit():
                                continue
                            board.push_san(tok)
                            seen.append(_key(board))
                    except (ValueError, KeyError):
                        continue
                    theory.update(seen)
                    plies = len(board.move_stack)
                    k = _key(board)
                    if k not in out or plies < out[k][2]:  # the shortest line naming this position is the canonical one
                        out[k] = (row["eco"], row["name"], plies)
        _theory.update(theory)
        _index = out
        return out


def identify(moves: list[str]) -> dict:
    """{'eco', 'name', 'ply', 'left_book'} for a game given in UCI. left_book is the first ply (0-based) whose
    position isn't on any named line, or None if the game never left theory (in its first 40 plies)."""
    idx = index()
    board = chess.Board()
    found = None
    left = None
    for ply, uci in enumerate(moves[:MAX_PLIES]):
        try:
            board.push(chess.Move.from_uci(uci))
        except ValueError:
            break
        k = _key(board)
        hit = idx.get(k)
        if hit:
            found = {"eco": hit[0], "name": hit[1], "ply": ply}
        if k in _theory:
            if hit:
                left = None  # back in theory (a transposition)
        elif left is None:
            left = ply
    out = found or {"eco": "", "name": "", "ply": None}
    out["left_book"] = left if found else 0
    return out


def in_book(board: chess.Board) -> bool:
    """Is this position on a named opening line (named itself, or on the way to one)?"""
    index()
    return _key(board) in _theory
