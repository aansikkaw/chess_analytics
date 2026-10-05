"""Getting games in: Lichess API download and PGN parsing.

Output of this module is a list of `GameRecord`s: plain data, no engine work yet.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn
import httpx

LICHESS_GAMES_URL = "https://lichess.org/api/games/user/{username}"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")


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
    played_at: str
    moves: list[str] = field(default_factory=list)  # UCI, in order
    clocks: list[float | None] = field(default_factory=list)  # seconds left after each ply

    @property
    def player_score(self) -> float:
        """1 for a win, 0.5 draw, 0 loss, from the user's side."""
        if self.result == "1/2-1/2":
            return 0.5
        white_won = self.result == "1-0"
        return 1.0 if white_won == (self.player_color == chess.WHITE) else 0.0


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise GameImportError("Usernames are 2–30 letters, digits, '_' or '-'.")
    return username


def fetch_lichess_pgn(username: str, max_games: int, timeout: float = 60.0) -> str:
    """Download a user's recent rated rapid/classical/blitz games as PGN with clock times."""
    username = validate_username(username)
    params = {
        "max": max_games,
        "rated": "true",
        "perfType": "blitz,rapid,classical",
        "clocks": "true",
        "opening": "true",
        "evals": "false",
    }
    headers = {"Accept": "application/x-chess-pgn"}
    try:
        resp = httpx.get(
            LICHESS_GAMES_URL.format(username=username),
            params=params,
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise GameImportError(f"Couldn't reach Lichess: {exc}") from exc
    if resp.status_code == 404:
        raise GameImportError(f"No Lichess user called '{username}'.")
    if resp.status_code == 429:
        raise GameImportError("Lichess is rate-limiting us. Wait a minute and try again.")
    resp.raise_for_status()
    return resp.text


_TC_RE = re.compile(r"^(\d+)\+(\d+)$")


def _initial_seconds(time_control: str) -> int | None:
    m = _TC_RE.match(time_control or "")
    return int(m.group(1)) if m else None


def _game_id(game: chess.pgn.Game) -> str:
    site = game.headers.get("Site", "")
    if "lichess.org/" in site:
        return site.rsplit("/", 1)[-1][:12]
    # No stable id (e.g. pasted OTB game): hash the headers + moves.
    raw = str(game.headers) + " ".join(m.uci() for m in game.mainline_moves())
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _rating(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def parse_pgn(pgn_text: str, username: str) -> list[GameRecord]:
    """Parse every game in `pgn_text` in which `username` played one side.

    Games the user didn't play, unfinished games and games from a custom
    starting position are skipped.
    """
    if not pgn_text or not pgn_text.strip():
        raise GameImportError("The PGN is empty.")
    who = username.strip().lower()
    records: list[GameRecord] = []
    stream = io.StringIO(pgn_text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        h = game.headers
        if h.get("Result") not in {"1-0", "0-1", "1/2-1/2"}:
            continue
        if "FEN" in h or h.get("Variant", "Standard").lower() not in {"standard", ""}:
            continue
        if game.errors:  # python-chess collects illegal/unparseable moves here
            continue
        white, black = h.get("White", "?"), h.get("Black", "?")
        if who == white.lower():
            color = chess.WHITE
        elif who == black.lower():
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
        records.append(
            GameRecord(
                game_id=_game_id(game),
                white=white,
                black=black,
                player_color=color,
                player_rating=_rating(h.get("WhiteElo" if color == chess.WHITE else "BlackElo")),
                result=h["Result"],
                time_control=tc,
                initial_seconds=_initial_seconds(tc),
                opening=h.get("Opening", h.get("ECO", "Unknown")),
                played_at=h.get("UTCDate", h.get("Date", "")),
                moves=moves,
                clocks=clocks,
            )
        )
    if not records:
        raise GameImportError(
            f"Found no finished standard games played by '{username}' in that PGN."
        )
    return records
