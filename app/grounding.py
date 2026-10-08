"""Answer verification: every move, evaluation and concept link the coach states must
come from knowledge it actually read (OKF concepts, the brief, or a live engine call).

What counts as a *claim*:
  * piece moves, captures, castling and numbered pawn moves ("Nf3", "exd5", "O-O", "12.d5");
  * evaluations like "+1.4" / "-0.7";
  * markdown links to concepts, which must resolve to an existing concept in the bundle.
Bare squares ("the e4 pawn") are prose, not move claims, so they aren't checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .okf import LINK_RE

PIECE_MOVE_RE = re.compile(r"(?<![A-Za-z0-9])(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?)[+#]?")
NUMBERED_PAWN_RE = re.compile(r"\b\d{1,3}\.(?:\.\.)?\s?([a-h][1-8](?:=[QRBN])?)[+#]?(?![a-z0-9])")
BARE_PAWN_RE = re.compile(r"(?<![A-Za-z0-9])([a-h][1-8](?:=[QRBN])?)(?![A-Za-z0-9])")
EVAL_RE = re.compile(r"(?<![\w.])([+-]\d{1,2}\.\d)(?!\d)")


def moves_in(text: str) -> set[str]:
    """Moves an answer *asserts*."""
    return set(PIECE_MOVE_RE.findall(text)) | set(NUMBERED_PAWN_RE.findall(text))


def concept_links(text: str) -> list[tuple[str, str]]:
    """(label, concept id) for bundle-absolute links in an answer."""
    out = []
    for label, target, _ in LINK_RE.findall(text):
        if target.startswith("/"):
            out.append((label, target[:-3]))
    return out


@dataclass
class Grounding:
    known_concepts: set[str] = field(default_factory=set)  # every concept id that exists
    allowed_moves: set[str] = field(default_factory=set)
    allowed_evals: set[str] = field(default_factory=set)

    def add_context(self, text: str) -> None:
        self.allowed_moves |= set(PIECE_MOVE_RE.findall(text)) | set(BARE_PAWN_RE.findall(text))
        self.allowed_evals |= set(EVAL_RE.findall(text))

    def check(self, answer: str) -> dict:
        bad_moves = sorted(mv for mv in moves_in(answer) if mv not in self.allowed_moves)
        bad_evals = sorted(e for e in set(EVAL_RE.findall(answer)) if e not in self.allowed_evals)
        bad_links = sorted({cid for _, cid in concept_links(answer) if cid not in self.known_concepts})
        return {"ok": not (bad_moves or bad_evals or bad_links), "moves": bad_moves, "evals": bad_evals, "links": bad_links}


def repair_instruction(problems: dict) -> str:
    parts = []
    if problems["moves"]:
        parts.append(f"moves not found in anything you read: {', '.join(problems['moves'])}")
    if problems["evals"]:
        parts.append(f"evaluations not found in anything you read: {', '.join(problems['evals'])}")
    if problems["links"]:
        parts.append(f"links to concepts that don't exist: {', '.join(problems['links'])}")
    return (
        "Grounding check failed. Your draft contains " + "; ".join(parts) + ". "
        "Rewrite the answer using only moves, evaluations and concept links that appear in what you read. "
        "If you need a move that isn't there, call analyse_position on the relevant FEN first. "
        "Reply with the full corrected answer only."
    )


def strip_unknown_links(answer: str, known: set[str]) -> str:
    """Turn links to non-existent concepts into plain text (keep the label)."""
    def repl(m):
        target = m.group(2)
        if target.startswith("/") and target[:-3] not in known:
            return m.group(1)
        return m.group(0)
    return LINK_RE.sub(repl, answer)
