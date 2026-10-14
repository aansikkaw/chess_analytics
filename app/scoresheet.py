"""Scoresheet scanner: a photo (or typed copy) of an over-the-board scoresheet -> a legal game.

Reading handwriting is the easy half; a vision model does it. The hard half is that handwriting is ambiguous
(is that "Nf6" or "Nf8"? "b5" or "65"? "Bb5" or "Bh5"?) and scoresheets have slips. The rules of chess fix
most of that: at every move only 20-40 moves are legal, so we decode the whole game at once.

    1. Read: the vision model returns each move as written, with up to two alternative readings where the
       ink is unclear (or you type the moves yourself: any language, any notation).
    2. Normalise: piece letters from English, German (K D T L S), French (R D T F C), Spanish/Italian (R D T A C),
       Portuguese (R D T B C), Dutch (K D T L P), Russian (Кр Ф Л С К) and figurines; 0-0 and O-O; x, : and × for
       captures; long algebraic (Ng1-f3, e2e4); annotations (+ # ! ?) are dropped.
    3. Match: each legal move is written out the ways people write it (SAN, without the x, without a needed
       disambiguation, long algebraic) and compared with the reading by a weighted edit distance in which
       look-alike characters (b/6, g/9, B/8, l/1, O/0, S/5, e/c...) cost little.
    4. Decode: a beam search over the whole game keeps the most likely legal sequences, so a smudged move is
       settled by the moves after it (only one reading lets the game continue).
    5. Confidence: each move is "sure", "check" or "guess" from how far ahead the chosen move is of the next
       legal candidate. You confirm the uncertain ones on the board, and the game is re-decoded from there.

Photos get more than one look (v1.2):
    * the browser flattens the sheet (perspective), removes shadows and sends zoomed crops of each half with it;
    * an optional second vision model (VISION2_*) reads it too, and the readings are merged by vote;
    * moves still unclear after decoding go back to the model as multiple choice ("which of Nf3, Nh3, Ng5 is
      written at move 14 for White?") - open reading turned into a much easier question;
    * a human-move prior from Stockfish breaks the remaining ties: players rarely write down a move that
      throws the game away, so P(move | sheet) is weighed with P(move | position) (a noisy-channel decoder).

The model's output is untrusted data: it only ever chooses among legal moves.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

import chess
import chess.engine
import chess.pgn
import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

LANGS: dict[str, dict[str, str]] = {
    "en": {"K": "K", "Q": "Q", "R": "R", "B": "B", "N": "N"},
    "de": {"K": "K", "D": "Q", "T": "R", "L": "B", "S": "N"},
    "fr": {"R": "K", "D": "Q", "T": "R", "F": "B", "C": "N"},
    "es": {"R": "K", "D": "Q", "T": "R", "A": "B", "C": "N"},  # also Italian
    "pt": {"R": "K", "D": "Q", "T": "R", "B": "B", "C": "N"},
    "nl": {"K": "K", "D": "Q", "T": "R", "L": "B", "P": "N"},
    "ru": {"Ф": "Q", "Л": "R", "С": "B", "К": "N"},  # "Кр" (king) is handled first
}
LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish / Italian", "pt": "Portuguese", "nl": "Dutch",
              "ru": "Russian"}
FIGURINES = str.maketrans({"♔": "K", "♕": "Q", "♖": "R", "♗": "B", "♘": "N", "♚": "K", "♛": "Q", "♜": "R", "♝": "B", "♞": "N"})
RESULT_TOKENS = {"1-0", "0-1", "1/2-1/2", "½-½", "1/2", "*", "0.5-0.5", "=", "draw"}

# Look-alike characters in handwriting: substituting one for the other is cheap.
_CONFUSE = ["b6", "bh", "g9", "gq", "B8", "BR", "l1", "I1", "i1", "O0", "o0", "S5", "s5", "Z2", "z2", "ec", "ea", "ad", "ao",
            "hn", "hk", "ft", "fl", "17", "49", "38", "56", "35", "ce", "uv", "nm", "QO", "QD", "Q0", "KR", "KX", "NH", "Nh", "Nn",
            "Bb", "Rr", "Kk", "Qq", "Dd", "Tt", "Ll", "Ss", "Cc", "Ff", "Aa", "Pp", "dc", "gy", "x+", "4a", "1l", "3f"]
CONFUSE = {(a, b) for a, b in _CONFUSE} | {(b, a) for a, b in _CONFUSE}
SUB_SIMILAR, SUB_OTHER, INDEL, INDEL_LIGHT = 0.4, 1.0, 0.85, 0.05
LIGHT = set("x-")  # a missing or extra capture mark or dash hardly matters
LAMBDA = 2.4  # log-likelihood per unit of edit distance
ALT_PRIOR = (0.0, -0.6, -1.0)  # the model's first reading is the likeliest
SKIP_PENALTY = -5.0  # treat a reading as noise (a crossed-out or stray entry)
BLANK_SCORE = -3.0  # an empty cell: any legal move, chosen by the rest of the game
BEAM, PER_STATE = 8, 5
PRIOR_W = 0.5  # weight of the human-move prior (log-probability relative to the most natural move)
PRIOR_STATES = 3  # the prior asks the engine about the best few beam states only (the rest get NO_PRIOR)
NO_PRIOR = -1.0
VERIFY_PRIOR = 0.8  # a reading confirmed by the multiple-choice second look
MAX_VERIFY = 14
MAX_PLIES = 400


# ---- normalising what's written ---------------------------------------------------------------------------
_COORD = re.compile(r"^[a-h][1-8][a-h][1-8][QRBN]?$")
_MOVE_NO = re.compile(r"^\d+\s*(\.{1,3}|…|\))\s*")
_CASTLE_LONG = re.compile(r"^[0oO][-–—]?[0oO][-–—]?[0oO]")
_CASTLE_SHORT = re.compile(r"^[0oO][-–—]?[0oO]")


def norm(token: str, lang: str = "en") -> str:
    """A written move -> canonical English-letter form (e.g. 'Sf3' in German -> 'Nf3', '0-0' -> 'O-O', 'e:d5' -> 'exd5')."""
    t = (token or "").strip().translate(FIGURINES)
    t = _MOVE_NO.sub("", t).strip()
    if not t:
        return ""
    if _CASTLE_LONG.match(t):
        return "O-O-O"
    if _CASTLE_SHORT.match(t) and not re.match(r"^0[-–—]?1", t):
        return "O-O"
    t = re.sub(r"(e\.?p\.?|ch|dbl)$", "", t, flags=re.I)
    t = re.sub(r"[+#!?†‡\s]", "", t)
    t = t.replace("×", "x").replace(":", "x").replace("*", "x").replace("–", "-").replace("—", "-")
    if lang == "ru":
        t = t.replace("Кр", "K")
    pieces = LANGS.get(lang, LANGS["en"])
    out = []
    for i, ch in enumerate(t):
        if ch in pieces and (i == 0 or (i == len(t) - 1 and (t[i - 1] in "=/(" or t[i - 1].isdigit()))):
            out.append(pieces[ch])  # a piece letter: at the start, or a promotion piece at the end
        elif ch == "X" and i > 0:
            out.append("x")
        else:
            out.append(ch)
    t = "".join(out)
    t = re.sub(r"[=/()]", "", t)  # e8=Q, e8/Q, e8(Q) -> e8Q
    return t


def forms(board: chess.Board, move: chess.Move, san: str | None = None) -> list[tuple[str, float]]:
    """The ways a legal move is written, each with a small cost for the less standard ones."""
    san = (san or board.san(move)).rstrip("+#").replace("=", "")
    out = [(san, 0.0)]
    frm, to = chess.square_name(move.from_square), chess.square_name(move.to_square)
    if san.startswith("O-O"):
        return out + [(frm + to, 0.2), ("K" + frm + to, 0.2)]  # castling in coordinates: e1g1, Ke1-g1
    piece = board.piece_at(move.from_square)
    promo = chess.piece_symbol(move.promotion).upper() if move.promotion else ""
    letter = "" if piece.piece_type == chess.PAWN else piece.symbol().upper()
    cap = board.is_capture(move)
    if letter:
        short = letter + ("x" if cap else "") + to  # omitted disambiguation (very common on scoresheets)
        if short != san:
            out.append((short, 0.3))
    elif cap:
        out.append((frm[0] + to, 0.1))  # "ed5" for exd5
        out.append((frm[0] + to[0], 0.6))  # "ed" (old short form)
    out.append((letter + frm + ("x" if cap else "-") + to + promo, 0.1))  # long algebraic: Ng1-f3, e2-e4, e7xd8Q
    out.append((frm + to + promo, 0.2))  # bare coordinates: e2e4
    return out


@lru_cache(maxsize=200_000)
def distance(a: str, b: str) -> float:
    """Weighted edit distance with cheap look-alike substitutions and nearly free x / - edits."""
    la, lb = len(a), len(b)
    prev = [0.0] * (lb + 1)
    for j in range(1, lb + 1):
        prev[j] = prev[j - 1] + (INDEL_LIGHT if b[j - 1] in LIGHT else INDEL)
    for i in range(1, la + 1):
        cur = [prev[0] + (INDEL_LIGHT if a[i - 1] in LIGHT else INDEL)] + [0.0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cb = b[j - 1]
            sub = 0.0 if ca == cb else SUB_SIMILAR if (ca, cb) in CONFUSE else SUB_OTHER
            cur[j] = min(prev[j - 1] + sub, prev[j] + (INDEL_LIGHT if ca in LIGHT else INDEL),
                         cur[j - 1] + (INDEL_LIGHT if cb in LIGHT else INDEL))
        prev = cur
    return prev[lb]


def match_cost(reading: str, board: chess.Board, move: chess.Move, cache: dict, san: str | None = None) -> float:
    """How far a reading is from the nearest way of writing this legal move (0 = written exactly so)."""
    san = san or board.san(move)
    key = (reading, san, move.uci())  # the long forms depend on the squares, so the move is part of the key
    hit = cache.get(key)
    if hit is not None:
        return hit
    r = reading.replace("-", "") if not reading.startswith("O-O") else reading
    best = min(distance(r, f if f.startswith("O-O") else f.replace("-", "")) + extra for f, extra in forms(board, move, san))
    cache[key] = best
    return best


# ---- reading tokens ------------------------------------------------------------------------------------------
def tokens_from_text(text: str) -> list[list[str]]:
    """Typed moves ("1. e4 e5 2. Sf3 Sc6 ...", any language) -> one reading per ply."""
    text = re.sub(r"\{[^}]*\}|\([^)]*\)|;[^\n]*", " ", text or "")  # comments and variations
    text = re.sub(r"\[[^\]]*\]", " ", text)  # PGN headers
    text = re.sub(r"(\d+)\s*(\.{1,3}|…)", r" \1. ", text)
    out = []
    for raw in re.split(r"[\s,]+", text):
        t = raw.strip()
        if not t or re.fullmatch(r"\d+\.?|\.+|…", t) or t in RESULT_TOKENS or t.lower() in RESULT_TOKENS:
            continue
        if re.fullmatch(r"[+#!?]+|e\.p\.?", t):
            continue
        out.append([t[:16]])
    return out[:MAX_PLIES]


def detect_language(tokens: list[list[str]]) -> list[str]:
    """Which notation languages fit the piece letters used, best first."""
    letters = [alts[0].strip().translate(FIGURINES)[:2] for alts in tokens if alts and alts[0].strip()]
    if any(re.search("[А-Яа-я]", x) for x in letters):
        return ["ru"]
    firsts = [x[0] for x in letters if x and x[0].isalpha() and x[0].isupper() and x[0] not in "O"]
    if not firsts:
        return ["en"]
    scores = []
    for lang, pieces in LANGS.items():
        if lang == "ru":
            continue
        fit = sum(f in pieces for f in firsts) / len(firsts)
        scores.append((fit, lang == "en", lang))
    scores.sort(reverse=True)
    best = scores[0][0]
    return [lang for fit, _, lang in scores if fit >= best - 0.05][:3]


# ---- decoding ---------------------------------------------------------------------------------------------------
@dataclass
class Ply:
    san: str
    uci: str
    raw: str
    read: str
    cost: float
    confidence: str
    alternatives: list[dict] = field(default_factory=list)
    locked: bool = False
    token: int = 0  # which entry on the sheet this move was read from (locks refer to it)
    checked: bool = False  # confirmed by the photo reader's second, multiple-choice look


@dataclass
class Decoded:
    language: str
    plies: list[Ply]
    fens: list[str]
    skipped: list[int]
    unused: int
    total: float

    def to_dict(self) -> dict:
        counts = {"sure": 0, "check": 0, "guess": 0}
        for p in self.plies:
            counts[p.confidence] += 1
        b = chess.Board()
        return {"language": self.language, "language_name": LANG_NAMES.get(self.language, self.language),
                "moves": [p.__dict__ | {"ply": i} for i, p in enumerate(self.plies)], "fens": self.fens,
                "skipped": self.skipped, "unused": self.unused, "counts": counts,
                "movetext": b.variation_san([chess.Move.from_uci(p.uci) for p in self.plies]) if self.plies else ""}


def decode(tokens: list[list[str]], locked: dict[int, str] | None = None, language: str = "auto", priors: list[list[float]] | None = None,
           prior_fn: Callable | None = None, prior_on: set[int] | None = None, checked: set[int] | None = None) -> Decoded:
    """The most likely legal game for these readings (one list of alternative readings per ply).
    `locked` maps a sheet entry (token index) to the move you confirmed for it (UCI). `priors` gives each
    alternative reading's log-weight (default: by rank). `prior_fn(board)` -> (logp by UCI, floor) adds a
    human-move prior on the entries in `prior_on`. `checked` marks entries confirmed by a second look."""
    tokens = [[(t or "")[:16] for t in alts[:4]] or [""] for alts in tokens[:MAX_PLIES]]
    if priors is not None:
        priors = [list(p[:len(t)]) + [ALT_PRIOR[-1]] * max(0, len(t) - len(p)) for t, p in zip(tokens, priors + [[]] * len(tokens), strict=False)]
    locked = locked or {}
    langs = detect_language(tokens) if language == "auto" else [language if language in LANGS else "en"]
    best: Decoded | None = None
    for lang in langs:
        d = _decode(tokens, locked, lang, priors, prior_fn, prior_on or set(), checked or set())
        if best is None or d.total > best.total:
            best = d
    return best


def _decode(tokens: list[list[str]], locked: dict[int, str], lang: str, priors: list[list[float]] | None = None,
            prior_fn: Callable | None = None, prior_on: set[int] | None = None, checked: set[int] | None = None) -> Decoded:
    prior_on, checked = prior_on or set(), checked or set()
    cache: dict = {}
    readings = [[norm(t, lang) for t in alts] for alts in tokens]
    # A beam state: (score, board, plies, skipped). plies hold (move, cost, raw, read, alternatives-with-scores)
    beam: list[tuple[float, chess.Board, list, list]] = [(0.0, chess.Board(), [], [])]
    unused = 0
    for i, alts in enumerate(readings):
        nxt = []
        for rank, (score, board, plies, skipped) in enumerate(beam):
            if board.is_game_over():
                nxt.append((score - 0.5, board, plies, skipped))  # tokens after the end: ignore them (counted below)
                continue
            scored = []
            want = locked.get(i)
            prior_here = prior_fn is not None and i in prior_on and not want
            human, floor = prior_fn(board) if prior_here and rank < PRIOR_STATES else ({}, NO_PRIOR if prior_here else 0.0)
            for mv in board.legal_moves:
                if want:
                    s, cost, raw, read = (0.0, 0.0, tokens[i][0], "locked") if mv.uci() == want else (-50.0, 9.0, tokens[i][0], "")
                elif not any(alts):
                    s, cost, raw, read = BLANK_SCORE, 3.0, "", ""
                else:
                    s, cost, raw, read = -1e9, 9.0, "", ""
                    san = board.san(mv)
                    for k, r in enumerate(alts):
                        if not r:
                            continue
                        c = match_cost(r, board, mv, cache, san)
                        v = -LAMBDA * c + (priors[i][k] if priors is not None else ALT_PRIOR[min(k, 2)])
                        if v > s:
                            s, cost, raw, read = v, c, tokens[i][k], r
                    if prior_here:
                        s += PRIOR_W * human.get(mv.uci(), floor)
                scored.append((s, cost, raw, read, mv))
            if not scored:
                continue
            scored.sort(key=lambda x: -x[0])
            alt_view = [(x[0], x[4]) for x in scored[:6]]
            for s, cost, raw, read, mv in scored[:PER_STATE]:
                b2 = board.copy(stack=False)
                b2.push(mv)
                nxt.append((score + s, b2, plies + [(mv, cost, raw, read, alt_view, bool(want), i)], skipped))
            if any(alts) and not want:
                nxt.append((score + SKIP_PENALTY, board, plies, skipped + [i]))
        if not nxt:
            break
        nxt.sort(key=lambda x: -x[0])
        dedup, seen = [], set()
        for st in nxt:  # different paths to the same position: keep the best
            k = (st[1].fen(), len(st[2]))
            if k in seen:
                continue
            seen.add(k)
            dedup.append(st)
            if len(dedup) >= BEAM:
                break
        beam = dedup
    score, board, plies, skipped = beam[0]
    out, fens = [], [chess.STARTING_FEN]
    b = chess.Board()
    for mv, cost, raw, read, alt_view, was_locked, tok in plies:
        top = alt_view[0][0]
        runner = next((s for s, m in alt_view if m != mv), top - 10)
        mine = next((s for s, m in alt_view if m == mv), top)
        margin = mine - runner
        if was_locked:
            conf = "sure"
        elif not raw:
            conf = "guess"
        elif (cost <= 0.2 and margin >= 0.8) or (margin >= 1.8 and cost <= 0.6):
            conf = "sure"  # written exactly as this move (and nothing else is close), or far ahead of the rest
        elif margin >= 0.7 and cost <= 1.3:
            conf = "check"
        else:
            conf = "guess"
        alts = [{"san": b.san(m), "uci": m.uci()} for _, m in alt_view[:5]]
        if tok in checked and not was_locked and conf != "sure" and cost <= 0.6:
            conf = "sure"  # the reader picked this move when shown the legal options
        if tok in checked and _COORD.match(raw or "") and len(tokens[tok]) > 1:
            raw = tokens[tok][1]  # show what's written on the sheet, not the answer to the second look
        out.append(Ply(san=b.san(mv), uci=mv.uci(), raw=raw, read=read, cost=round(cost, 2), confidence=conf, alternatives=alts,
                       locked=was_locked, token=tok, checked=tok in checked))
        b.push(mv)
        fens.append(b.fen())
    used = len(plies) + len(skipped)
    unused = max(0, len(tokens) - used)
    return Decoded(language=lang, plies=out, fens=fens, skipped=skipped, unused=unused, total=score)


# ---- reading a photo with a vision model ------------------------------------------------------------------------
GROQ_VISION_DEFAULT = "qwen/qwen3.8-27b"
MAX_IMAGE_BYTES = 6 * 1024 * 1024
PROMPT = """This is a photo of a handwritten chess scoresheet. Transcribe it exactly as written.

Return only a JSON object:
{"white": "", "black": "", "event": "", "date": "", "round": "", "result": "",
 "moves": [{"n": 1, "white": ["e4"], "black": ["e5"]}, {"n": 2, "white": ["Nf3"], "black": ["Nc6", "Nc8"]}]}

Rules:
- One entry per numbered row, in order. "white" and "black" are lists: the most likely reading first, then up to
  two other readings ONLY if the handwriting is unclear. Use [] for an empty cell.
- Copy each move as written: keep the writer's language for piece letters (e.g. S, L, T, D in German; C, F, T, D
  in French), keep 0-0 or O-O, x or :. Don't translate, correct or complete moves; never invent moves.
- Skip crossed-out entries. Leave a field "" if you can't read it. Result like "1-0", "0-1" or "1/2-1/2"."""


@dataclass(frozen=True)
class VisionBackend:
    kind: str  # anthropic | openai
    model: str
    base_url: str | None = None
    api_key: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "anthropic":
            return f"Claude · {self.model}"
        from .llm import PROVIDER_NAMES

        host = (self.base_url or "").split("//")[-1].split("/")[0]
        return f"{next((v for k, v in PROVIDER_NAMES.items() if k in host), host)} · {self.model}"


def vision_backend(s) -> VisionBackend | None:
    """Which model reads photos: VISION_* settings first, then Claude, then Groq's vision model with the Groq key."""
    p = (s.vision_provider or "auto").lower()
    if p == "off":
        return None
    if p in ("auto", "openai") and s.vision_base_url and s.vision_model:
        return VisionBackend("openai", s.vision_model, s.vision_base_url.rstrip("/"), s.vision_api_key or s.llm_api_key)
    if p in ("auto", "anthropic") and s.anthropic_api_key:
        return VisionBackend("anthropic", s.vision_model or s.coach_model, api_key=s.anthropic_api_key)
    if p in ("auto", "openai") and s.llm_base_url and "groq.com" in s.llm_base_url and s.llm_api_key:
        return VisionBackend("openai", s.vision_model or GROQ_VISION_DEFAULT, s.llm_base_url, s.llm_api_key)
    return None


def vision_backends(s) -> list[VisionBackend]:
    """The photo readers: the main one, plus a second one if VISION2_* is set (their readings are merged)."""
    out = []
    main = vision_backend(s)
    if main:
        out.append(main)
        if s.vision2_model and s.vision2_base_url:
            second = VisionBackend("openai", s.vision2_model, s.vision2_base_url, s.vision2_api_key)
            if second != main:
                out.append(second)
    return out


class VisionError(RuntimeError):
    pass


class SheetRow(BaseModel):
    n: int | None = None
    white: list[str] = Field(default_factory=list)
    black: list[str] = Field(default_factory=list)

    @field_validator("white", "black", mode="before")
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            return [str(x)[:16] for x in v if isinstance(x, str | int | float) and str(x).strip()][:3]
        return []

    @field_validator("n", mode="before")
    @classmethod
    def _n(cls, v):
        try:
            return int(str(v).strip(". "))
        except (TypeError, ValueError):
            return None


class Sheet(BaseModel):
    white: str = ""
    black: str = ""
    event: str = ""
    date: str = ""
    round: str = ""
    result: str = ""
    moves: list[SheetRow] = Field(default_factory=list, max_length=250)

    @field_validator("white", "black", "event", "date", "round", "result", mode="before")
    @classmethod
    def _text(cls, v):
        return str(v or "").strip()[:60]

    def cells(self) -> list[tuple[tuple[int, str], list[str]]]:
        """((move number, colour), readings) in game order; blank cells kept, except at the very end."""
        rows = sorted(enumerate(self.moves), key=lambda x: (x[1].n if x[1].n is not None else 10_000 + x[0], x[0]))
        out: list[tuple[tuple[int, str], list[str]]] = []
        for idx, r in rows:
            n = r.n if r.n is not None else idx + 1
            out.append(((n, "white"), r.white or [""]))
            if r.black:
                out.append(((n, "black"), r.black))
            elif r is not rows[-1][1]:
                out.append(((n, "black"), [""]))
        while out and out[-1][1] == [""]:
            out.pop()
        return out[:MAX_PLIES]

    def tokens(self) -> list[list[str]]:
        return [alts for _, alts in self.cells()]

    def headers(self) -> dict:
        result = self.result.replace("½", "1/2").replace(" ", "")
        result = {"1/2": "1/2-1/2", "0.5-0.5": "1/2-1/2"}.get(result, result)
        return {"white": self.white, "black": self.black, "event": self.event, "date": self.date, "round": self.round,
                "result": result if result in ("1-0", "0-1", "1/2-1/2") else ""}


def parse_sheet(text: str) -> Sheet:
    """The model's reply -> a validated Sheet. Tolerates prose or code fences around the JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise VisionError("The photo reader didn't return a transcription. Try a sharper, flatter photo.")
    try:
        return Sheet.model_validate(json.loads(text[start:end + 1]))
    except (ValueError, ValidationError) as exc:
        raise VisionError("The photo reader's transcription wasn't readable. Try again, or type the moves.") from exc


def image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


CROPS_NOTE = ("\n\nImage 1 is the whole sheet. The next images are zoomed-in copies of its left and right halves "
              "(the sheet's two move columns): read the handwriting from the zoomed copies, and use image 1 for the order.")


def transcribe(data: bytes | list[bytes], backend: VisionBackend, http: httpx.Client | None = None, timeout: float = 90.0) -> tuple[Sheet, int]:
    """Read a scoresheet photo (or the whole sheet followed by zoomed crops). Returns the sheet and the tokens used."""
    images = data if isinstance(data, list) else [data]
    prompt = PROMPT + (CROPS_NOTE if len(images) > 1 else "")
    text, tokens = vision_call(images, prompt, backend, http, timeout)
    return parse_sheet(text), tokens


def vision_call(images: list[bytes], prompt: str, backend: VisionBackend, http: httpx.Client | None = None,
                timeout: float = 90.0) -> tuple[str, int]:
    """One request with images and a prompt; returns (text, tokens used)."""
    parts = []
    for data in images[:3]:  # some providers take at most three images per request
        mime = image_type(data)
        if not mime:
            raise VisionError("That isn't a JPEG, PNG or WebP image.")
        if len(data) > MAX_IMAGE_BYTES:
            raise VisionError("That photo is too large. Take it again at a lower resolution.")
        parts.append((mime, base64.b64encode(data).decode()))
    own = http is None
    http = http or httpx.Client(timeout=timeout)
    try:
        if backend.kind == "anthropic":
            content = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": b}} for m, b in parts]
            r = _post(http, "https://api.anthropic.com/v1/messages",
                      {"x-api-key": backend.api_key or "", "anthropic-version": "2023-06-01"},
                      {"model": backend.model, "max_tokens": 4096, "temperature": 0,
                       "messages": [{"role": "user", "content": [*content, {"type": "text", "text": prompt}]}]})
            body = r.json()
            text = "".join(c.get("text", "") for c in body.get("content", []) if isinstance(c, dict))
            usage = body.get("usage") or {}
            return text, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": f"data:{m};base64,{b}"}} for m, b in parts]
        payload = {"model": backend.model, "temperature": 0, "max_tokens": 4096, "response_format": {"type": "json_object"},
                   "messages": [{"role": "user", "content": content}]}
        headers = {"Authorization": f"Bearer {backend.api_key}"} if backend.api_key else {}
        try:
            r = _post(http, f"{backend.base_url}/chat/completions", headers, payload)
        except VisionError as exc:
            if not any(k in str(exc).lower() for k in ("response_format", "json", "(400)", "(422)")):
                raise
            payload.pop("response_format")  # a provider that can't combine JSON mode with images: the prompt asks for JSON anyway
            r = _post(http, f"{backend.base_url}/chat/completions", headers, payload)
        body = r.json()
        try:
            text = body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionError("The photo reader returned something unexpected. Try again.") from exc
        return text, int((body.get("usage") or {}).get("total_tokens", 0))
    finally:
        if own:
            http.close()


# ---- several readers, merged by vote ----------------------------------------------------------------------------
RANK_WEIGHT = (1.0, 0.5, 0.3, 0.2)


def merge_sheets(sheets: list[Sheet]) -> tuple[list[list[str]], list[list[float]], list[tuple[int, str]], list[int]]:
    """(tokens, priors, slots, votes): each cell's readings from every reader, weighted by rank and agreement.
    votes[i] = how many readers put the winning reading first."""
    cells: dict[tuple[int, str], dict[str, float]] = {}
    firsts: dict[tuple[int, str], list[str]] = {}
    for sh in sheets:
        for slot, alts in sh.cells():
            d = cells.setdefault(slot, {})
            for r, t in enumerate(alts[:4]):
                t = t.strip()
                if t:
                    d[t] = d.get(t, 0.0) + RANK_WEIGHT[r]
            if alts and alts[0].strip():
                firsts.setdefault(slot, []).append(alts[0].strip())
    slots = sorted(cells, key=lambda k: (k[0], k[1] == "black"))
    tokens, priors, votes = [], [], []
    for slot in slots:
        d = cells[slot]
        if not d:
            tokens.append([""])
            priors.append([0.0])
            votes.append(0)
            continue
        ranked = sorted(d.items(), key=lambda kv: -kv[1])[:4]
        top = ranked[0][1]
        tokens.append([t for t, _ in ranked])
        priors.append([max(-2.0, math.log(w / top)) for _, w in ranked])
        votes.append(firsts.get(slot, []).count(ranked[0][0]))
    while tokens and tokens[-1] == [""]:
        tokens.pop(), priors.pop(), slots.pop(), votes.pop()
    return tokens, priors, slots, votes


def merge_headers(sheets: list[Sheet]) -> dict:
    out = {}
    for sh in sheets:
        for k, v in sh.headers().items():
            if v and not out.get(k):
                out[k] = v
    return {k: out.get(k, "") for k in ("white", "black", "event", "date", "round", "result")}


# ---- a second, multiple-choice look at the unclear moves --------------------------------------------------------
VERIFY_PROMPT = """You transcribed this chess scoresheet before. A few moves were unclear. For each question below, look
at that move on the sheet again and pick which option matches what is written. The options are the legal moves
there, written in English notation (K king, Q queen, R rook, B bishop, N knight); the sheet may use another
language's letters ({lang}). Answer "unclear" if none matches.
Return only JSON: {{"answers": [{{"id": 1, "choice": "Nf3"}}, ...]}}

Questions:
"""


def verify(images: list[bytes], backend: VisionBackend, tokens: list[list[str]], priors: list[list[float]], slots: list[tuple[int, str]],
           decoded: Decoded, http: httpx.Client | None = None) -> tuple[list[list[str]], list[list[float]], set[int], int]:
    """Ask again about the unclear moves, as multiple choice among legal moves. Returns updated tokens and priors,
    the entries that were confirmed, and the tokens used."""
    unsure = [(k, p) for k, p in enumerate(decoded.plies) if p.confidence != "sure" and not p.locked and p.token < len(slots)][:MAX_VERIFY]
    if not unsure:
        return tokens, priors, set(), 0
    questions = []
    for q, (_, p) in enumerate(unsure, 1):
        n, color = slots[p.token]
        opts = [a["san"] for a in p.alternatives[:5]]
        if p.san not in opts:
            opts = [p.san, *opts[:4]]
        questions.append({"id": q, "move": f"move {n}, {color}", "we_read": p.raw or "(blank)", "options": opts})
    text, used = vision_call(images, VERIFY_PROMPT.format(lang=LANG_NAMES.get(decoded.language, "English")) + json.dumps(questions),
                             backend, http)
    start, end = text.find("{"), text.rfind("}")
    try:
        answers = json.loads(text[start:end + 1]).get("answers", []) if start >= 0 else []
    except ValueError:
        answers = []
    tokens = [list(t) for t in tokens]
    priors = [list(p) for p in priors]
    confirmed: set[int] = set()
    by_id = {q["id"]: (unsure[q["id"] - 1], q) for q in questions}
    for a in answers if isinstance(answers, list) else []:
        if not isinstance(a, dict) or a.get("id") not in by_id:
            continue
        (k, ply), q = by_id[a["id"]]
        choice = str(a.get("choice", "")).strip()
        if not choice or choice.lower() == "unclear":
            continue
        try:  # one of the options, or any other legal move there
            uci = chess.Board(decoded.fens[k]).parse_san(choice).uci()
        except ValueError:
            continue
        coord = uci[:4] + uci[4:].upper()  # coordinates read the same in every notation language
        i = ply.token
        rest = [(t, w) for t, w in zip(tokens[i], priors[i], strict=False) if t != coord][:3]
        tokens[i] = [coord] + [t for t, _ in rest]
        priors[i] = [VERIFY_PRIOR] + [w - 0.2 for _, w in rest]
        confirmed.add(i)
    return tokens, priors, confirmed, used


# ---- a human-move prior from Stockfish -------------------------------------------------------------------------------
def engine_prior(engine, depth: int = 8) -> Callable:
    """P(move | position) for a club player, roughly: the engine's top moves, each less likely the more it loses
    (a 10-point loss in expected score halves the odds); anything else gets a small floor. Cached per position.
    Returns log-probabilities relative to the most natural move (so it's 0 for that move)."""
    from .analysis import win_percent

    cache: dict[str, tuple[dict[str, float], float]] = {}
    limit = chess.engine.Limit(depth=depth, time=0.25)

    def fn(board: chess.Board) -> tuple[dict[str, float], float]:
        k = board.epd()
        hit = cache.get(k)
        if hit is not None:
            return hit
        try:
            cands = engine.candidates(board, 8, limit)
        except Exception:  # noqa: BLE001 - a busy or broken engine just means no prior
            cands = []
        if not cands:
            cache[k] = ({}, 0.0)
            return cache[k]
        best = win_percent(cands[0][1])
        out = {u: -0.07 * (best - win_percent(cp)) for u, cp in cands}
        cache[k] = (out, min(out.values()) - 1.0)
        return cache[k]

    return fn


def read_photo(images: list[bytes], backends: list[VisionBackend], engine=None, http: httpx.Client | None = None) -> dict:
    """The whole photo pipeline: read with every reader, merge, decode, re-ask about unclear moves, decode again
    with the human-move prior. Raises VisionError if no reader could read it."""
    sheets, used, errors, readers = [], 0, [], []
    for b in backends:
        try:
            sh, t = transcribe(images, b)
        except VisionError as exc:
            errors.append(exc)
            continue
        sheets.append(sh)
        readers.append(b)
        used += t
    if not sheets:
        raise errors[0] if errors else VisionError("No photo reader is set up.")
    tokens, priors, slots, votes = merge_sheets(sheets)
    if not tokens:
        return {"sheet": merge_headers(sheets), "tokens": [], "priors": [], "slots": [], "decoded": None, "used": used,
                "readers": [b.label for b in readers], "checked": 0}
    first = decode(tokens, priors=priors)
    checked: set[int] = set()
    try:
        tokens, priors, checked, t = verify(images, readers[0], tokens, priors, slots, first, http)
        used += t
    except VisionError:
        pass  # the second look is a bonus; the first reading stands
    unsure = {p.token for p in first.plies if p.confidence != "sure"}
    final = decode(tokens, language=first.language, priors=priors, prior_fn=engine_prior(engine) if engine is not None and unsure else None,
                   prior_on=unsure, checked=checked)
    return {"sheet": merge_headers(sheets), "tokens": tokens, "priors": priors, "slots": [list(x) for x in slots], "votes": votes,
            "decoded": final, "used": used, "readers": [b.label for b in readers], "checked": len(checked)}


def _post(http: httpx.Client, url: str, headers: dict, payload: dict) -> httpx.Response:
    try:
        r = http.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise VisionError("Reading the photo took too long. Try again, or type the moves.") from exc
    except httpx.HTTPError as exc:
        raise VisionError("Couldn't reach the photo reader. Try again, or type the moves.") from exc
    if r.status_code in (401, 403):
        raise VisionError("The photo reader's API key was rejected. (Owner: check VISION_API_KEY / the provider key.)")
    if r.status_code == 429:
        raise VisionError("The photo reader is busy (rate limit). Wait a minute and try again.")
    if r.status_code == 404:
        raise VisionError("The photo reader's model isn't available. (Owner: set VISION_MODEL to a vision model.)")
    if r.status_code >= 400:
        raise VisionError(f"The photo reader returned an error ({r.status_code}): {r.text[:160]}")
    return r


# ---- writing the game ---------------------------------------------------------------------------------------------
def build_pgn(ucis: list[str], white: str, black: str, result: str, date: str = "", event: str = "", round_: str = "") -> str:
    """A PGN for the confirmed moves. Raises ValueError on an illegal move."""
    board = chess.Board()
    game = chess.pgn.Game()
    clean = lambda v, d="?": re.sub(r'["\\\]\[]', "", (v or "").strip())[:60] or d  # noqa: E731
    game.headers["Event"] = clean(event, "Over-the-board game")
    game.headers["Site"] = "Over the board"
    game.headers["Date"] = _pgn_date(date)
    game.headers["Round"] = clean(round_)
    game.headers["White"] = clean(white)
    game.headers["Black"] = clean(black)
    game.headers["Result"] = result if result in ("1-0", "0-1", "1/2-1/2") else "*"
    game.headers["Annotator"] = "Plateau Breaker scoresheet scanner"
    node = game
    for u in ucis:
        mv = chess.Move.from_uci(u)
        if mv not in board.legal_moves:
            raise ValueError(f"Move {len(board.move_stack) + 1} isn't legal.")
        node = node.add_variation(mv)
        board.push(mv)
    return str(game) + "\n"


def _pgn_date(d: str) -> str:
    d = (d or "").strip()
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$", d)
    if m:
        return f"{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$", d)  # 25/09/2026: day first, as on most scoresheets
    if m:
        return f"{m.group(3)}.{int(m.group(2)):02d}.{int(m.group(1)):02d}"
    return "????.??.??"

