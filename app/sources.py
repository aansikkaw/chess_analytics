"""Fetch games straight from Lichess and Chess.com. No copy-pasting PGN.

Both are free public APIs that need no key:
  * Lichess:   GET /api/games/user/{user}  -> streamed PGN (with clocks)
  * Chess.com: GET /pub/player/{user}/games/archives -> monthly archive URLs,
               each archive -> JSON with a `pgn` field per game

Both platforms ask for *serial* access (one request at a time) and a pause after
a 429. Requests to each platform therefore go through a per-platform lock, and
after a 429 that platform is paused for a minute for everyone. In production the web
process and the worker process both call these APIs, so the lock is also a file lock
and the pause is also a file, both in the data folder the processes share.

Every function takes an optional `httpx.Client` so tests can inject a mock.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path

try:
    import fcntl  # Linux/macOS; on Windows the in-process lock alone is used
except ImportError:  # pragma: no cover
    fcntl = None

import httpx

from .games import TIME_CLASSES, GameImportError, GameRecord, parse_pgn, validate_username

PLATFORMS = ("lichess", "chesscom")
LICHESS = "https://lichess.org"
CHESSCOM = "https://api.chess.com/pub"
# Chess.com asks API users to identify themselves with contact details in the User-Agent.
USER_AGENT = f"PlateauBreaker/1.0 (contact: {os.getenv('CONTACT_EMAIL', 'not-set')})"
TIMEOUT = httpx.Timeout(60.0, connect=20.0)  # TLS handshakes from cloud machines are sometimes slow
COOLDOWN_S = 60.0
NETWORK_RETRIES = 2  # extra attempts after a timeout or dropped connection
RETRY_BACKOFF_S = (2.0, 5.0)
_pause = time.sleep  # swapped out in tests
_TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)

_locks = {"lichess": threading.Lock(), "chesscom": threading.Lock()}
_paused_until: dict[str, float] = {"lichess": 0.0, "chesscom": 0.0}


class RateLimited(GameImportError):
    """The platform asked us to slow down; nothing more is sent to it for a minute."""


def _shared_dir() -> Path:
    return Path(os.getenv("DB_PATH", "plateau.db")).resolve().parent


def _pause_file(platform: str) -> Path:
    return _shared_dir() / f".{platform}.paused-until"


def _paused_until_any(platform: str) -> float:
    """The later of this process's pause and the one another process wrote to the shared file."""
    try:
        shared = float(_pause_file(platform).read_text().strip() or 0)
    except (OSError, ValueError):
        shared = 0.0
    return max(_paused_until[platform], shared)


@contextmanager
def _file_lock(platform: str):
    if fcntl is None:
        yield
        return
    try:
        fh = open(_shared_dir() / f".{platform}.lock", "a")  # noqa: SIM115 - held for the duration of the request
    except OSError:
        yield
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@contextmanager
def _serial(platform: str, cross_process: bool = True):
    """One request at a time per platform (across processes too), and respect a recent 429."""
    wait = _paused_until_any(platform) - time.time()
    if wait > 0:
        name = "Lichess" if platform == "lichess" else "Chess.com"
        raise RateLimited(f"{name} asked us to slow down. Try again in {int(wait) + 1} seconds.")
    with _locks[platform], (_file_lock(platform) if cross_process else nullcontext()):
        yield


def _note_429(platform: str) -> None:
    until = time.time() + COOLDOWN_S
    _paused_until[platform] = until
    with suppress(OSError):
        _pause_file(platform).write_text(str(until))


def _get(c: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with a couple of retries for network hiccups (timeouts, handshakes, dropped connections)."""
    for attempt in range(NETWORK_RETRIES + 1):
        try:
            return c.get(url, **kw)
        except _TRANSIENT:
            if attempt == NETWORK_RETRIES:
                raise
            _pause(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
    raise AssertionError("unreachable")


def network_error(name: str, exc: httpx.HTTPError) -> GameImportError:
    """A plain-English message instead of a raw SSL/socket error."""
    if isinstance(exc, httpx.TimeoutException):
        why = "the connection timed out"
    elif isinstance(exc, httpx.ConnectError):
        why = "the connection failed"
    else:
        why = "the connection dropped"
    return GameImportError(f"Couldn't reach {name} ({why}, even after retrying). Its servers may be busy; "
                           "try again in a minute or two.")


def _client(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def _clean_classes(time_classes: Iterable[str] | None) -> list[str]:
    classes = [c for c in (time_classes or ("blitz", "rapid", "classical")) if c in TIME_CLASSES]
    if not classes:
        raise GameImportError(f"Pick at least one time control: {', '.join(TIME_CLASSES)}.")
    return classes


# ---- account checks -------------------------------------------------------------
def check_account(platform: str, username: str, http: httpx.Client | None = None) -> str:
    """Confirm the account exists. Returns the canonical username (correct case)."""
    username = validate_username(username)
    if platform not in PLATFORMS:
        raise GameImportError(f"Unknown platform '{platform}'.")
    c = _client(http)
    try:
        with _serial(platform):
            return _check(platform, username, c)
    except httpx.HTTPError as exc:
        raise network_error("Lichess" if platform == "lichess" else "Chess.com", exc) from exc
    finally:
        if http is None:
            c.close()


def _check(platform: str, username: str, c: httpx.Client) -> str:
    if platform == "lichess":
        r = _get(c, f"{LICHESS}/api/user/{username}")
        if r.status_code == 404:
            raise GameImportError(f"No Lichess account called '{username}'. Check the spelling on your profile page.")
        _raise_for(r, "Lichess")
        data = r.json()
        if data.get("closed") or data.get("disabled"):
            raise GameImportError(f"The Lichess account '{username}' is closed, so its games aren't available.")
        return data.get("username") or username
    if platform == "chesscom":
        r = _get(c, f"{CHESSCOM}/player/{username.lower()}")
        if r.status_code in (404, 410):
            raise GameImportError(f"No Chess.com account called '{username}'.")
        _raise_for(r, "Chess.com")
        url = r.json().get("url", "")
        return url.rstrip("/").rsplit("/", 1)[-1] if url else username
    raise GameImportError(f"Unknown platform '{platform}'.")


def _raise_for(r: httpx.Response, name: str) -> None:
    if r.status_code == 429:
        _note_429("lichess" if name == "Lichess" else "chesscom")
        raise RateLimited(f"{name} is rate-limiting requests. Wait a minute and try again.")
    if r.status_code >= 400:
        raise GameImportError(f"{name} returned an error ({r.status_code}). Try again shortly.")


# ---- game download ----------------------------------------------------------------
def fetch_lichess(username: str, max_games: int, time_classes=None, since_ms: int | None = None,
                  http: httpx.Client | None = None) -> str:
    username = validate_username(username)
    params = {
        "max": max_games,
        "rated": "true",
        "perfType": ",".join(_clean_classes(time_classes)),
        "clocks": "true",
        "opening": "true",
        "evals": "false",
    }
    if since_ms:
        params["since"] = since_ms
    c = _client(http)
    try:
        with _serial("lichess"):
            r = _get(c, f"{LICHESS}/api/games/user/{username}", params=params, headers={"Accept": "application/x-chess-pgn"})
            if r.status_code == 404:
                # The export says 404 both for unknown users and some edge cases; ask the profile API which it is.
                _check("lichess", username, c)
                raise GameImportError("Lichess wouldn't return games for this account. Try again later or use Paste PGN.")
            _raise_for(r, "Lichess")
            return r.text
    except httpx.HTTPError as exc:
        raise network_error("Lichess", exc) from exc
    finally:
        if http is None:
            c.close()


def fetch_chesscom(username: str, max_games: int, time_classes=None, since_ms: int | None = None,
                   http: httpx.Client | None = None, max_archives: int = 60) -> str:
    """Walk monthly archives newest-first until we have `max_games` matching games."""
    username = validate_username(username).lower()
    wanted = set(_clean_classes(time_classes))
    # Chess.com has no "classical" class: its long live games are labelled "rapid".
    if "classical" in wanted:
        wanted.add("rapid")
    since_s = (since_ms or 0) / 1000
    c = _client(http)
    try:
        with _serial("chesscom"):
            return _walk_archives(c, username, max_games, wanted, since_s, max_archives)
    except httpx.HTTPError as exc:
        raise network_error("Chess.com", exc) from exc
    finally:
        if http is None:
            c.close()


def _walk_archives(c: httpx.Client, username: str, max_games: int, wanted: set, since_s: float, max_archives: int) -> str:
    r = _get(c, f"{CHESSCOM}/player/{username}/games/archives")
    if r.status_code in (404, 410):
        raise GameImportError(f"No Chess.com account called '{username}'.")
    _raise_for(r, "Chess.com")
    archives = list(reversed(r.json().get("archives", [])))[:max_archives]
    pgns: list[str] = []
    for url in archives:
        try:
            a = _get(c, url)
        except httpx.HTTPError:
            if pgns:  # keep what we already have rather than failing the whole import
                break
            raise
        if a.status_code == 429 and pgns:
            _note_429("chesscom")
            break
        _raise_for(a, "Chess.com")
        games = sorted(a.json().get("games", []), key=lambda g: g.get("end_time", 0), reverse=True)
        stop = False
        for g in games:
            if g.get("end_time", 0) <= since_s:
                stop = True
                break
            if g.get("rules") != "chess" or not g.get("rated") or g.get("time_class") not in wanted:
                continue
            if g.get("pgn"):
                pgns.append(g["pgn"])
            if len(pgns) >= max_games:
                stop = True
                break
        if stop:
            break
    return "\n\n".join(pgns)


def fetch_games(platform: str, username: str, max_games: int, time_classes=None, since_ms: int | None = None,
                http: httpx.Client | None = None) -> list[GameRecord]:
    """Download and parse. Returns [] (not an error) when there's simply nothing new."""
    if platform == "lichess":
        pgn = fetch_lichess(username, max_games, time_classes, since_ms, http)
    elif platform == "chesscom":
        pgn = fetch_chesscom(username, max_games, time_classes, since_ms, http)
    else:
        raise GameImportError(f"Unknown platform '{platform}'.")
    if not pgn.strip():
        if since_ms:
            return []
        raise GameImportError(
            f"No rated {', '.join(_clean_classes(time_classes))} games found for '{username}'. "
            "Try including more time controls (e.g. bullet)."
        )
    try:
        return parse_pgn(pgn, username)[:max_games]
    except GameImportError:
        if since_ms:
            return []
        raise
