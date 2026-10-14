"""The puzzle bank: puzzles from the Lichess puzzle database (CC0), prepared for Twin predictions.

Lichess publishes about five million rated puzzles as one CSV (database.lichess.org). Each row is:
    PuzzleId, FEN, Moves, Rating, RatingDeviation, Popularity, NbPlays, Themes, GameUrl, OpeningTags
FEN is the position *before* the opponent's move; Moves starts with that move, then alternates solver /
opponent and ends on the solver's move. So the puzzle the player sees is FEN + Moves[0].

`scripts/import_puzzles.py` loads a balanced selection (the same number per rating band, well-played
puzzles only). A small sample of our own (sample_data/puzzles_sample.csv) is loaded on first use, so a
fresh install works before that import.

Annotation (shared by everyone)
-------------------------------
To ask "would *this* player find the move?", the Twin needs the same thing it learns from in real games:
Stockfish's candidate moves in the position, with how much each loses, and the Twin's features for each.
That costs an engine search, but it doesn't depend on who's asking. So each puzzle is annotated once
(candidate rows packed into `feats`), and scoring any player's Twin on it afterwards is just arithmetic:
thousands of puzzles per second.
"""

from __future__ import annotations

import csv
import io
import json
import random
import subprocess
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import chess
import chess.engine

from . import twin as twin_mod
from .analysis import game_phase

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "puzzles_sample.csv"
LICHESS_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
FEATS_VERSION = 1  # bump with twin.VERSION changes: annotations are then redone
BAND = 50  # rating band width for balanced imports


# Lichess theme keys -> what a player reads. Unknown keys are shown as written.
THEME_LABEL = {
    "advancedPawn": "Advanced pawn", "advantage": "Winning advantage", "anastasiaMate": "Anastasia's mate", "arabianMate": "Arabian mate",
    "attackingF2F7": "Attacking f2/f7", "attraction": "Attraction", "backRankMate": "Back-rank mate", "bishopEndgame": "Bishop endgame",
    "bodenMate": "Boden's mate", "capturingDefender": "Capture the defender", "castling": "Castling", "clearance": "Clearance",
    "crushing": "Crushing", "defensiveMove": "Defensive move", "deflection": "Deflection", "discoveredAttack": "Discovered attack",
    "doubleBishopMate": "Two-bishop mate", "doubleCheck": "Double check", "dovetailMate": "Dovetail mate", "enPassant": "En passant",
    "endgame": "Endgame", "equality": "Hold the balance", "exposedKing": "Exposed king", "fork": "Fork", "hangingPiece": "Hanging piece",
    "hookMate": "Hook mate", "interference": "Interference", "intermezzo": "In-between move", "kingsideAttack": "Kingside attack",
    "knightEndgame": "Knight endgame", "long": "Three moves", "mate": "Checkmate", "mateIn1": "Mate in 1", "mateIn2": "Mate in 2",
    "mateIn3": "Mate in 3", "mateIn4": "Mate in 4", "mateIn5": "Mate in 5+", "middlegame": "Middlegame", "oneMove": "One move",
    "opening": "Opening", "pawnEndgame": "Pawn endgame", "pin": "Pin", "promotion": "Promotion", "queenEndgame": "Queen endgame",
    "queenRookEndgame": "Queen and rook endgame", "queensideAttack": "Queenside attack", "quietMove": "Quiet move",
    "rookEndgame": "Rook endgame", "sacrifice": "Sacrifice", "short": "Two moves", "skewer": "Skewer", "smotheredMate": "Smothered mate",
    "trappedPiece": "Trapped piece", "underPromotion": "Underpromotion", "veryLong": "Four moves or more", "xRayAttack": "X-ray attack",
    "zugzwang": "Zugzwang",
}
# Themes that say nothing about the idea (length, outcome, source): not shown as the puzzle's "motif".
META_THEMES = {"oneMove", "short", "long", "veryLong", "advantage", "crushing", "master", "masterVsMaster", "superGM", "mate"}
PHASES = ("opening", "middlegame", "endgame")
ENDGAME_THEMES = {"bishopEndgame", "knightEndgame", "pawnEndgame", "queenEndgame", "queenRookEndgame", "rookEndgame"}


def theme_label(key: str) -> str:
    return THEME_LABEL.get(key, key)


def motif_themes(themes: Iterable[str]) -> list[str]:
    return [t for t in themes if t not in META_THEMES and t not in PHASES]


# ---- parsing and importing -----------------------------------------------------------------------------
def parse_row(row: dict, source: str = "lichess") -> dict | None:
    """One CSV row -> a bank row, or None if it isn't usable (bad FEN, illegal move, odd length)."""
    try:
        moves = row["Moves"].split()
        if len(moves) < 2 or len(moves) % 2 != 0:  # opponent's move + an odd-length solver line
            return None
        setup = chess.Board(row["FEN"])
        mv0 = chess.Move.from_uci(moves[0])
        if mv0 not in setup.legal_moves:
            return None
        board = setup.copy()
        board.push(mv0)
        fen = board.fen()
        sans = []
        for u in moves[1:]:
            m = chess.Move.from_uci(u)
            if m not in board.legal_moves:
                return None
            sans.append(board.san(m))
            board.push(m)
        themes = row.get("Themes", "").split()
        phase = next((p for p in PHASES if p in themes), None) or game_phase(chess.Board(fen))
        return {"id": row["PuzzleId"].strip()[:16], "setup_fen": setup.fen(), "setup_uci": moves[0], "fen": fen,
                "moves": " ".join(moves[1:]), "sans": " ".join(sans), "rating": int(row["Rating"]),
                "rd": int(row.get("RatingDeviation") or 80), "popularity": int(row.get("Popularity") or 0),
                "plays": int(row.get("NbPlays") or 0), "themes": " ".join(themes), "phase": phase,
                "game_url": (row.get("GameUrl") or "")[:120], "opening": (row.get("OpeningTags") or "")[:120], "source": source}
    except (KeyError, ValueError, TypeError):
        return None


def import_rows(store, rows: Iterable[dict], *, source: str = "lichess", min_popularity: int = -100, min_plays: int = 0,
                max_rd: int = 500, rating_min: int = 0, rating_max: int = 4000, per_band: int | None = None,
                limit: int | None = None, progress=None) -> int:
    """Load puzzles, keeping at most `per_band` per 50-point rating band so the bank covers every level evenly.
    Returns how many new puzzles were added."""
    with store._conn() as c:
        counts: dict[int, int] = {}
        if per_band:
            for r in c.execute("SELECT rating / ? AS b, COUNT(*) AS n FROM puzzle_bank GROUP BY b", (BAND,)):
                counts[r["b"]] = r["n"]
    added, seen, batch = 0, 0, []

    def flush():
        nonlocal added, batch
        if not batch:
            return
        with store._conn() as c:
            before = c.total_changes
            c.executemany("INSERT OR IGNORE INTO puzzle_bank (id, setup_fen, setup_uci, fen, moves, sans, rating, rd, popularity, plays, "
                          "themes, phase, game_url, opening, source) VALUES (:id, :setup_fen, :setup_uci, :fen, :moves, :sans, :rating, :rd, "
                          ":popularity, :plays, :themes, :phase, :game_url, :opening, :source)", batch)
            added += c.total_changes - before
        batch = []

    for raw in rows:
        seen += 1
        try:  # cheap numeric filters first: most rows are rejected here, before any chess parsing
            rating = int(raw["Rating"])
            if not (rating_min <= rating <= rating_max) or int(raw.get("Popularity") or 0) < min_popularity \
                    or int(raw.get("NbPlays") or 0) < min_plays or int(raw.get("RatingDeviation") or 0) > max_rd:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        band = rating // BAND
        if per_band and counts.get(band, 0) >= per_band:
            continue
        row = parse_row(raw, source)
        if row is None:
            continue
        batch.append(row)
        counts[band] = counts.get(band, 0) + 1
        if len(batch) >= 2000:
            flush()
            if progress:
                progress(seen, added)
        if limit and added + len(batch) >= limit:
            break
    flush()
    return added


def read_csv(path: str | Path) -> Iterator[dict]:
    """Rows from a puzzle CSV: plain, or zstandard-compressed (.zst) via the `zstandard` module or the `zstd` tool."""
    path = Path(path)
    if path.suffix == ".zst":
        yield from csv.DictReader(_zst_text(path.open("rb")))
        return
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def _zst_text(raw) -> io.TextIOBase:
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw), encoding="utf-8", newline="")
    try:
        proc = subprocess.Popen(["zstd", "-dc"], stdin=raw, stdout=subprocess.PIPE)  # noqa: S603,S607 - fixed command
    except FileNotFoundError as exc:
        raise RuntimeError("To read .zst files, run `pip install zstandard` (or install the zstd tool).") from exc
    return io.TextIOWrapper(proc.stdout, encoding="utf-8", newline="")


def ensure_bank(store) -> int:
    """Load the bundled sample the first time the bank is used, so a fresh install has puzzles. Returns the bank size."""
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM puzzle_bank").fetchone()[0]
    if n == 0 and SAMPLE.exists():
        import_rows(store, read_csv(SAMPLE), source="sample")
        with store._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM puzzle_bank").fetchone()[0]
    return n


def stats(store) -> dict:
    with store._conn() as c:
        r = c.execute("SELECT COUNT(*) AS n, SUM(feats IS NOT NULL AND feats_v = ?) AS ready, MIN(rating) AS lo, MAX(rating) AS hi, "
                      "SUM(source = 'sample') AS sample FROM puzzle_bank", (FEATS_VERSION,)).fetchone()
    return {"puzzles": r["n"] or 0, "annotated": r["ready"] or 0, "rating_min": r["lo"], "rating_max": r["hi"],
            "sample_only": bool(r["n"]) and (r["sample"] or 0) == r["n"]}


def get(store, puzzle_id: str) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT * FROM puzzle_bank WHERE id = ?", (puzzle_id,)).fetchone()
    return dict(r) if r else None


# ---- annotation: the engine's candidates for the solver's first move, packed once for everyone ---------------
def pack_feats(ucis: list[str], cps: list[int], rows: list[list[float]], sol: int, top: int) -> bytes:
    sparse = [[[k, round(v, 3)] for k, v in enumerate(r) if v and round(v, 3)] for r in rows]
    return zlib.compress(json.dumps({"u": ucis, "cp": cps, "r": sparse, "s": sol, "t": top}, separators=(",", ":")).encode(), 6)


def unpack_feats(blob: bytes) -> dict:
    d = json.loads(zlib.decompress(blob))
    d["rows"] = [tuple((k, v) for k, v in r) for r in d.pop("r")]
    return d


def annotate_one(row: dict, engine, depth: int) -> bytes | None:
    """Stockfish's candidates (top 10 + two random moves + the solution) with the Twin's features for each."""
    board = chess.Board(row["fen"])
    sol = row["moves"].split()[0]
    rng = random.Random(row["id"])  # the same random extras every time for the same puzzle
    limit = chess.engine.Limit(depth=depth, time=1.0)
    cands = twin_mod.candidate_set(board, engine, limit, rng, must_include=sol)
    if not cands:
        return None
    opp_to = chess.Move.from_uci(row["setup_uci"]).to_square
    ucis, rows, _ = twin_mod.decision_rows(board, cands, False, None, opp_to)
    if sol not in ucis:
        return None
    top = ucis.index(max(cands, key=lambda x: x[1])[0])
    return pack_feats(ucis, [cp for _, cp in cands], rows, ucis.index(sol), top)


def annotate(store, engine, ids: list[str], depth: int, on_one=None) -> int:
    done = 0
    for i, pid in enumerate(ids, 1):
        row = get(store, pid)
        if row:
            blob = annotate_one(row, engine, depth)
            with store._conn() as c:
                c.execute("UPDATE puzzle_bank SET feats = ?, feats_v = ?, feats_depth = ? WHERE id = ?",
                          (blob or b"", FEATS_VERSION, depth, pid))
            done += blob is not None
        if on_one:
            on_one(i)
    return done


def band_counts(store, lo: int, hi: int) -> tuple[int, int]:
    """(annotated, total) puzzles rated lo..hi."""
    with store._conn() as c:
        r = c.execute("SELECT COUNT(*) AS n, SUM(feats IS NOT NULL AND length(feats) > 0 AND feats_v = ?) AS ready "
                      "FROM puzzle_bank WHERE rating BETWEEN ? AND ?", (FEATS_VERSION, lo, hi)).fetchone()
    return r["ready"] or 0, r["n"] or 0


def pick_unannotated(store, lo: int, hi: int, n: int, seed: int = 0) -> list[str]:
    """Up to n puzzles in the band that still need annotating, spread over the band (not just its bottom)."""
    with store._conn() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM puzzle_bank WHERE rating BETWEEN ? AND ? AND (feats IS NULL OR feats_v IS NOT ?) "
                                       "ORDER BY rating LIMIT 20000", (lo, hi, FEATS_VERSION))]
    if len(ids) <= n:
        return ids
    rng = random.Random(seed)
    return rng.sample(ids, n)


def annotated_in_band(store, lo: int, hi: int, exclude: set[str], limit: int = 6000) -> list[dict]:
    with store._conn() as c:
        rows = c.execute("SELECT * FROM puzzle_bank WHERE rating BETWEEN ? AND ? AND feats_v = ? AND feats IS NOT NULL AND length(feats) > 0 "
                         "LIMIT ?", (lo, hi, FEATS_VERSION, limit)).fetchall()
    return [dict(r) for r in rows if r["id"] not in exclude]

