"""Lichess / Chess.com fetching against a mock HTTP server (no network in tests)."""

import httpx
import pytest

from app.games import GameImportError, parse_pgn, time_class_of
import app.sources as sources
from app.sources import RateLimited, check_account, fetch_chesscom, fetch_games, fetch_lichess

PGN = """[Event "Rated rapid game"]
[Site "https://lichess.org/AbCd1234"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[UTCDate "2026.09.01"]
[WhiteElo "1900"]
[BlackElo "1880"]
[TimeControl "600+5"]
[Termination "Normal"]
[Opening "Italian Game"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 5. d4 exd4 6. cxd4 Bb4+ 1-0
"""

CC_PGN = PGN.replace('[Site "https://lichess.org/AbCd1234"]', '[Site "Chess.com"]\n[Link "https://www.chess.com/game/live/987654321"]') \
            .replace('[Opening "Italian Game"]', '[ECOUrl "https://www.chess.com/openings/Italian-Game-Giuoco-Piano-4.c3"]') \
            .replace('[TimeControl "600+5"]', '[TimeControl "180"]').replace('[Termination "Normal"]', '[Termination "bob won on time"]') \
            .replace('[Result "1-0"]', '[Result "0-1"]').replace("Bb4+ 1-0", "Bb4+ 0-1")


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """Retry back-offs are recorded, not slept."""
    waits = []
    monkeypatch.setattr(sources, "_pause", waits.append)
    return waits


def _clear_pauses():
    sources._paused_until.update({"lichess": 0.0, "chesscom": 0.0})
    for p in ("lichess", "chesscom"):
        sources._pause_file(p).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _no_cooldown():
    _clear_pauses()
    yield
    _clear_pauses()


def mock(routes):
    seen = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        for prefix, (status, body) in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(body, (dict, list)):
                    return httpx.Response(status, json=body)
                return httpx.Response(status, text=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_time_class_rule():
    assert time_class_of("60+0") == "bullet"
    assert time_class_of("180+2") == "blitz"
    assert time_class_of("600+5") == "rapid"
    assert time_class_of("1800+20") == "classical"
    assert time_class_of("1/86400") == "" and time_class_of("-") == ""


def test_parse_lichess_metadata():
    [g] = parse_pgn(PGN, "alice")
    assert g.game_id == "AbCd1234" and g.time_class == "rapid" and g.url == "https://lichess.org/AbCd1234"
    assert g.opening == "Italian Game"


def test_parse_chesscom_metadata():
    [g] = parse_pgn(CC_PGN, "alice")
    assert g.game_id == "cc987654321" and g.time_class == "blitz" and g.initial_seconds == 180
    assert g.opening == "Italian Game Giuoco Piano"  # from ECOUrl, stops at the move number
    assert g.lost_on_time and "chess.com/game/live" in g.url


def test_lichess_fetch_params_and_since():
    http, seen = mock({"https://lichess.org/api/games/user/alice": (200, PGN)})
    fetch_lichess("alice", 20, ["blitz", "rapid"], since_ms=1234, http=http)
    url = seen[0]
    assert "max=20" in url and "perfType=blitz%2Crapid" in url and "since=1234" in url and "clocks=true" in url


def test_lichess_404_distinguishes_missing_user():
    http, _ = mock({"https://lichess.org/api/games/user/ghost": (404, ""), "https://lichess.org/api/user/ghost": (404, {})})
    with pytest.raises(GameImportError, match="No Lichess account"):
        fetch_lichess("ghost", 10, http=http)


def test_lichess_closed_account():
    http, _ = mock({"https://lichess.org/api/user/gone": (200, {"id": "gone", "closed": True})})
    with pytest.raises(GameImportError, match="closed"):
        check_account("lichess", "gone", http)


def test_check_account_returns_canonical_case():
    http, _ = mock({"https://lichess.org/api/user/aanletsgo": (200, {"id": "aanletsgo", "username": "AanLetsGo"})})
    assert check_account("lichess", "aanletsgo", http) == "AanLetsGo"


def test_chesscom_walks_archives_newest_first_and_filters():
    games_new = {"games": [
        {"pgn": CC_PGN, "rules": "chess", "rated": True, "time_class": "blitz", "end_time": 2000},
        {"pgn": "IGNORED", "rules": "chess960", "rated": True, "time_class": "blitz", "end_time": 1990},
        {"pgn": "IGNORED", "rules": "chess", "rated": False, "time_class": "blitz", "end_time": 1980},
        {"pgn": "IGNORED", "rules": "chess", "rated": True, "time_class": "daily", "end_time": 1970},
    ]}
    http, seen = mock({
        "https://api.chess.com/pub/player/alice/games/archives": (200, {"archives": [
            "https://api.chess.com/pub/player/alice/games/2026/08", "https://api.chess.com/pub/player/alice/games/2026/09"]}),
        "https://api.chess.com/pub/player/alice/games/2026/09": (200, games_new),
        "https://api.chess.com/pub/player/alice/games/2026/08": (200, {"games": []}),
    })
    text = fetch_chesscom("alice", 1, ["blitz"], http=http)
    assert "IGNORED" not in text and "987654321" in text
    assert seen[1].endswith("2026/09")  # newest month first
    assert len(seen) == 2  # stopped once max_games was reached


def test_chesscom_since_stops_early():
    http, seen = mock({
        "https://api.chess.com/pub/player/alice/games/archives": (200, {"archives": ["https://api.chess.com/pub/player/alice/games/2026/09"]}),
        "https://api.chess.com/pub/player/alice/games/2026/09": (200, {"games": [
            {"pgn": CC_PGN, "rules": "chess", "rated": True, "time_class": "blitz", "end_time": 100}]}),
    })
    assert fetch_games("chesscom", "alice", 10, ["blitz"], since_ms=200_000, http=http) == []


def test_fetch_games_empty_explains_time_controls():
    http, _ = mock({"https://lichess.org/api/games/user/alice": (200, "")})
    with pytest.raises(GameImportError, match="bullet"):
        fetch_games("lichess", "alice", 10, ["rapid"], http=http)


def test_rate_limit_pauses_the_platform_for_everyone():
    http, seen = mock({"https://lichess.org/api/games/user/alice": (429, "")})
    with pytest.raises(RateLimited, match="rate-limiting"):
        fetch_lichess("alice", 10, http=http)
    with pytest.raises(RateLimited, match="slow down"):
        fetch_lichess("bob", 10, http=http)  # refused locally during the cooldown...
    assert len(seen) == 1  # ...without sending a second request to Lichess
    http2, _ = mock({"https://api.chess.com/pub/player/carol": (200, {"url": "https://www.chess.com/member/Carol"})})
    assert check_account("chesscom", "carol", http2) == "Carol"



# ---- network hiccups (e.g. "_ssl.c: The handshake operation timed out") -----------------------------
def flaky(fail_times, exc=httpx.ConnectTimeout("_ssl.c:993: The handshake operation timed out"), ok=None, fail_on=None):
    """A client whose requests time out `fail_times` times (only for URLs containing fail_on), then succeed."""
    seen, left = [], {"n": fail_times}

    def handler(request):
        seen.append(str(request.url))
        if (fail_on is None or fail_on in str(request.url)) and left["n"] > 0:
            left["n"] -= 1
            raise exc
        return ok(request)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_handshake_timeout_is_retried(pauses):
    http, seen = flaky(2, ok=lambda r: httpx.Response(200, json={"url": "https://www.chess.com/member/Carol"}))
    assert check_account("chesscom", "carol", http) == "Carol"
    assert len(seen) == 3 and pauses == list(sources.RETRY_BACKOFF_S)


def test_persistent_timeout_gives_a_plain_message(pauses):
    http, seen = flaky(99, ok=None)
    with pytest.raises(GameImportError, match=r"Couldn't reach Chess.com \(the connection timed out") as e:
        check_account("chesscom", "carol", http)
    assert "_ssl" not in str(e.value) and len(seen) == 1 + sources.NETWORK_RETRIES


def test_later_archive_failure_keeps_games_already_fetched():
    archives = {"archives": ["https://api.chess.com/pub/player/alice/games/2026/08",
                             "https://api.chess.com/pub/player/alice/games/2026/09"]}
    month = {"games": [{"pgn": CC_PGN, "rules": "chess", "rated": True, "time_class": "blitz", "end_time": 2000}]}

    def ok(r):
        return httpx.Response(200, json=archives if r.url.path.endswith("archives") else month)
    http, _ = flaky(99, ok=ok, fail_on="2026/08")  # the older month never loads
    games = fetch_games("chesscom", "alice", 10, ["blitz"], http=http)
    assert len(games) == 1


def test_rate_limit_pause_is_shared_between_processes():
    """The web process and the worker both call Lichess: a 429 seen by one pauses the other."""
    import subprocess
    import sys

    http, _ = mock({"https://lichess.org/api/games/user/alice": (429, "")})
    with pytest.raises(RateLimited):
        fetch_lichess("alice", 10, http=http)
    code = ("import app.sources as s, sys\n"
            "try:\n    s.check_account('lichess', 'bob')\nexcept s.RateLimited as e:\n    print('paused:', e); sys.exit(0)\nsys.exit(1)")
    root = str(sources.__file__).rsplit("/app/", 1)[0]
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root)  # same DB_PATH env
    assert r.returncode == 0 and "slow down" in r.stdout


def test_requests_hold_a_cross_process_lock():
    import fcntl

    with sources._serial("lichess"), open(sources._shared_dir() / ".lichess.lock", "a") as fh, pytest.raises(BlockingIOError):
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process would have to wait
