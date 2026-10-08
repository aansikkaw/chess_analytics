"""PGN parsing: turn raw PGN text (from any source) into `GameRecord`s.

Fetching from Lichess / Chess.com lives in `sources.py`. This module is pure
data handling, no network and no engine.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field

import chess
import chess.pgn

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
TIME_CLASSES = ("bullet", "blitz", "rapid", "classical")


class GameImportError(Exception):
    """Raised for problems the user can fix (bad username, empty PGN...)."""


@dataclass
class GameRecord:
    game_id: str
    white: str
    black: str
    player_color: chess.Color  # which side the user played
    player_rating: int | None
    result: str  # "1-0", "0-1", "1/2-1/2"
    time_control: str
    initial_seconds: int | None
    opening: str
    played_at: str  # "YYYY.MM.DD"
    moves: list[str] = field(default_factory=list)  # UCI, in order
    clocks: list[float | None] = field(default_factory=list)  # seconds left after each ply
    time_class: str = ""  # bullet / blitz / rapid / classical / "" if unknown
    termination: str = ""  # e.g. "Normal", "Time forfeit"
    url: str = ""

    @property
    def player_score(self) -> float:
        """1 for a win, 0.5 draw, 0 loss, from the user's side."""
        if self.result == "1/2-1/2":
            return 0.5
        white_won = self.result == "1-0"
        return 1.0 if white_won == (self.player_color == chess.WHITE) else 0.0

    @property
    def lost_on_time(self) -> bool:
        return self.player_score == 0.0 and "time" in self.termination.lower()


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def decode_upload(data: bytes, filename: str = "") -> str:
    """Turn an uploaded .pgn (or a .zip of .pgn files) into text.

    ChessBase and many Windows tools export PGN in Windows-1252 rather than UTF-8,
    so names like "Nepomniachtchi" or "Gukesh D" with accents would otherwise break.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise GameImportError(f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Export fewer games.")
    if filename.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = [m for m in z.infolist() if m.filename.lower().endswith(".pgn") and not m.is_dir()]
                if not members:
                    raise GameImportError("That zip has no .pgn files inside. Export your ChessBase database as PGN first.")
                if sum(m.file_size for m in members) > MAX_UPLOAD_BYTES * 4:
                    raise GameImportError("The PGN files in that zip are too large in total.")
                return "\n\n".join(_decode_bytes(z.read(m)) for m in members)
        except zipfile.BadZipFile as exc:
            raise GameImportError("That zip file is damaged.") from exc
    lower = filename.lower()
    if lower.endswith((".cbh", ".cbv", ".cbz", ".cbf", ".ctg", ".cbone")):
        raise GameImportError("That's a ChessBase database file. In ChessBase, copy the games into a new PGN database "
                              "(File > New > Database, choose PGN) and upload the .pgn file.")
    return _decode_bytes(data)


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _name_tokens(name: str) -> frozenset[str]:
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return frozenset(t for t in re.split(r"[^a-z0-9]+", norm) if t)


def name_matches(header_name: str, wanted: str) -> bool:
    """'Sikka, Aanya' matches 'Aanya Sikka' and 'aanya sikka'; a handle matches itself.

    A single-word name must match a whole header exactly, so 'Aanya' alone won't match 'Aanya Sharma'.
    """
    if header_name.strip().lower() == wanted.strip().lower():
        return True
    h, w = _name_tokens(header_name), _name_tokens(wanted)
    if not w or not h:
        return False
    return h == w or (len(w) >= 2 and w <= h)


def player_names(pgn_text: str, limit: int = 8) -> list[tuple[str, int]]:
    """Most frequent player names in a PGN (headers only, fast)."""
    names = Counter(re.findall(r'^\[(?:White|Black) "([^"]*)"\]', pgn_text, re.M))
    names.pop("?", None)
    names.pop("", None)
    return names.most_common(limit)


PGN_NAME_RE = re.compile(r"^[\w][\w .,'()-]{1,59}$", re.UNICODE)


def validate_pgn_name(name: str) -> str:
    """A player name as written in a PGN/ChessBase file, e.g. "Sikka, Aanya" or "Aanya Sikka"."""
    name = " ".join((name or "").split())
    if not PGN_NAME_RE.match(name):
        raise GameImportError("Enter your name as it appears in the file (2-60 characters), e.g. \"Sikka, Aanya\".")
    return name


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise GameImportError("Usernames are 2–30 letters, digits, '_' or '-'.")
    return username


_TC_RE = re.compile(r"^(\d+)(?:\+(\d+))?$")


def parse_time_control(time_control: str) -> tuple[int | None, int]:
    """'600+5' -> (600, 5); '180' -> (180, 0); '-' or '1/86400' (daily) -> (None, 0)."""
    m = _TC_RE.match((time_control or "").strip())
    if not m:
        return None, 0
    return int(m.group(1)), int(m.group(2) or 0)


def time_class_of(time_control: str) -> str:
    """Lichess's rule: estimated duration = base + 40 x increment."""
    base, inc = parse_time_control(time_control)
    if base is None:
        return ""
    est = base + 40 * inc
    if est < 180:
        return "bullet"
    if est < 480:
        return "blitz"
    if est < 1500:
        return "rapid"
    return "classical"


def _game_id(game: chess.pgn.Game) -> str:
    h = game.headers
    for key in ("Site", "Link"):
        url = h.get(key, "")
        if "lichess.org/" in url:
            return url.rstrip("/").rsplit("/", 1)[-1][:12]
        if "chess.com/game/" in url:
            return "cc" + url.rstrip("/").rsplit("/", 1)[-1][:14]
    # No stable id (e.g. pasted OTB game): hash the headers + moves.
    raw = str(h) + " ".join(m.uci() for m in game.mainline_moves())
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _game_url(h: chess.pgn.Headers) -> str:
    for key in ("Site", "Link"):
        url = h.get(key, "")
        if url.startswith("http") and ("lichess.org/" in url or "chess.com/" in url):
            return url
    return ""


def _opening_name(h: chess.pgn.Headers) -> str:
    if h.get("Opening") and h["Opening"] != "?":
        return h["Opening"]
    # Chess.com puts the name in a URL: .../openings/Sicilian-Defense-Najdorf-Variation-6.Be3
    eco_url = h.get("ECOUrl", "")
    if "/openings/" in eco_url:
        slug = eco_url.rsplit("/openings/", 1)[-1]
        words = []
        for part in slug.split("-"):
            if part[:1].isdigit():  # stop at move numbers like "6.Be3"
                break
            words.append(part)
        if words:
            return " ".join(words)
    return h.get("ECO", "Unknown")


def _rating(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def parse_pgn(pgn_text: str, username: str) -> list[GameRecord]:
    """Parse every game in `pgn_text` in which `username` played one side.

    Games the user didn't play, unfinished games, variants and games from a
    custom starting position are skipped.
    """
    if not pgn_text or not pgn_text.strip():
        raise GameImportError("The PGN is empty.")
    records: list[GameRecord] = []
    stream = io.StringIO(pgn_text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        h = game.headers
        if h.get("Result") not in {"1-0", "0-1", "1/2-1/2"}:
            continue
        if ("FEN" in h and h.get("SetUp") != "0") or h.get("Variant", "Standard").lower() not in {"standard", ""}:
            continue
        if game.errors:  # python-chess collects illegal/unparseable moves here
            continue
        white, black = h.get("White", "?"), h.get("Black", "?")
        if name_matches(white, username):
            color = chess.WHITE
        elif name_matches(black, username):
            color = chess.BLACK
        else:
            continue

        moves, clocks = [], []
        for node in game.mainline():
            moves.append(node.move.uci())
            clocks.append(node.clock())  # None when the PGN has no [%clk]
        if len(moves) < 10:  # too short to say anything useful
            continue

        tc = h.get("TimeControl", "-")
        base, _ = parse_time_control(tc)
        records.append(
            GameRecord(
                game_id=_game_id(game),
                white=white,
                black=black,
                player_color=color,
                player_rating=_rating(h.get("WhiteElo" if color == chess.WHITE else "BlackElo")),
                result=h["Result"],
                time_control=tc,
                initial_seconds=base,
                opening=_opening_name(h),
                played_at=h.get("UTCDate", h.get("Date", "")),
                moves=moves,
                clocks=clocks,
                time_class=time_class_of(tc),
                termination=h.get("Termination", ""),
                url=_game_url(h),
            )
        )
    if not records:
        top = ", ".join(f"{n} ({c})" for n, c in player_names(pgn_text, 5))
        raise GameImportError(f"Found no finished standard games played by '{username}' in that PGN."
                              + (f" Names in the file: {top}." if top else ""))
    return records
