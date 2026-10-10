"""v0.8 Opponent Library: saved players, incremental updates, search, position search, PGN upload/export."""

import io

import chess
import chess.pgn
import pytest

from app.games import parse_pgn
from app.library import Library, board_from, position_key
from tests.test_api import DEMO, client


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


@pytest.fixture
def fake_site(m, monkeypatch):
    """Lichess stand-in: returns the demo games, newest first, and records every call."""
    calls = []
    games = parse_pgn(DEMO, "demo_player")

    def fetch(platform, handle, n, classes, since_ms=None):
        calls.append({"handle": handle, "n": n, "since_ms": since_ms})
        return games[:n] if since_ms is None else games[:2]

    monkeypatch.setattr(m, "check_account", lambda platform, handle: handle)
    monkeypatch.setattr(m, "fetch_games", fetch)
    monkeypatch.setattr(m, "games_cap", lambda user: 500)  # the test server caps syncs at 3 games
    return calls


def _age(m, pid, seconds=7200):
    """Pretend the player was last downloaded a while ago."""
    with m.store._conn() as c:
        c.execute("UPDATE lib_players SET fetched_at = fetched_at - ? WHERE id = ?", (seconds, pid))


def _pro(m, email):
    c = client(m, email)
    m.store.set_plan(m.store.user_by_email(email)["id"], "pro")
    return c


# ---- storage ----------------------------------------------------------------------------------------
def test_storage_dedupes_and_indexes_positions(tmp_path):
    from app.store import Store

    lib = Library(Store(str(tmp_path / "lib.db")))
    p = lib.get_or_create_player("lichess", "Demo_Player")
    assert lib.get_or_create_player("lichess", "demo_player")["id"] == p["id"]  # handles are case-insensitive
    recs = parse_pgn(DEMO, "demo_player")[:8]
    assert lib.add_games(p["id"], recs, requested=8) == 8
    assert lib.add_games(p["id"], recs) == 0  # nothing new the second time
    assert lib.player(p["id"])["games"] == 8 and lib.player(p["id"])["requested"] == 8
    # The starting position is in every game; its moves add up to the games
    lib.save(1, p["id"])
    ex = lib.explore([p["id"]], chess.Board(), {})
    assert ex["games"] == 8 and sum(mv["games"] for mv in ex["moves"]) == 8
    assert all(mv["san"] and 0 <= mv["their_score_pct"] <= 100 for mv in ex["moves"])
    # Following the most common move narrows it down
    first = ex["moves"][0]
    after = lib.explore([p["id"]], board_from([first["uci"]]), {})
    assert after["games"] == first["games"]
    # Records round-trip exactly
    assert lib.records(p["id"])[0].moves in [r.moves for r in recs]


def test_position_search_finds_transpositions(tmp_path):
    from app.games import GameRecord
    from app.store import Store

    lib = Library(Store(str(tmp_path / "t.db")))
    p = lib.get_or_create_player("lichess", "x")

    def rec(gid, ucis):
        return GameRecord(gid, "x", "y", chess.WHITE, 1500, "1-0", "300+0", 300, "Test", "2026.01.01", ucis + ["a2a3"] * 0)
    # Same position after 1.Nf3 d5 2.d4 and 1.d4 d5 2.Nf3
    a = rec("a", ["g1f3", "d7d5", "d2d4", "g8f6", "c2c4", "e7e6", "b1c3", "f8e7", "c1g5", "e8g8"])
    b = rec("b", ["d2d4", "d7d5", "g1f3", "c8f5", "c2c4", "e7e6", "b1c3", "c7c6", "d1b3", "d8b6"])
    lib.add_games(p["id"], [a, b])
    target = board_from(["d2d4", "d7d5", "g1f3"])
    assert board_from(["g1f3", "d7d5", "d2d4"]).fen().split(" ")[:4] == target.fen().split(" ")[:4]
    ex = lib.explore([p["id"]], target, {})
    assert ex["games"] == 2 and {mv["san"] for mv in ex["moves"]} == {"Nf6", "Bf5"}
    assert position_key(target) == position_key(board_from(["g1f3", "d7d5", "d2d4"]))


def test_board_from_validates():
    assert board_from(["e4", "e5"]).fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3")
    with pytest.raises(ValueError):
        board_from(["e2e5"])
    with pytest.raises(ValueError):
        board_from(fen="8/8/8/8/8/8/8/8 w - - 0 1")


# ---- API ----------------------------------------------------------------------------------------------
def test_library_is_pro_only(m):
    with client(m, "lib-free@test.com") as c:
        assert c.get("/api/library").status_code == 402


def test_scout_saves_and_updates_incrementally(m, fake_site):
    with _pro(m, "lib1@test.com") as c:
        uid = m.store.user_by_email("lib1@test.com")["id"]
        r = c.post("/api/scout", json={"platform": "lichess", "handle": "inc_one", "max_games": 20}).json()
        assert r["downloaded"] and r["new_games"] == 20 and r["games"] == 20 and r["player"]["games"] == 20
        assert fake_site[-1]["since_ms"] is None and m.store.usage_today(uid, "scout") == 1
        # Searching again straight away opens the saved copy: no download, no daily-limit use
        r = c.post("/api/scout", json={"platform": "lichess", "handle": "inc_one", "max_games": 20}).json()
        assert r["downloaded"] is False and len(fake_site) == 1 and m.store.usage_today(uid, "scout") == 1
        lib = c.get("/api/library").json()
        assert [p["handle"] for p in lib["players"]] == ["inc_one"] and lib["limit"] == 150
        pid = lib["players"][0]["id"]
        # Opening the report from the library never downloads
        rep = c.get(f"/api/library/{pid}/report").json()
        assert rep["games"] == 20 and len(fake_site) == 1
        # "Update" fetches only new games (since the last fetch), even long after
        _age(m, pid)
        u = c.post(f"/api/library/{pid}/update").json()
        assert fake_site[-1]["since_ms"] is not None and u["new_games"] == 0 and u["message"] == "0 new games added."
        # Asking for more games than stored does a full download
        _age(m, pid)
        r = c.post("/api/library", json={"platform": "lichess", "handle": "inc_one", "max_games": 40}).json()
        assert fake_site[-1]["since_ms"] is None and r["player"]["games"] == 40


def test_shared_cache_between_users(m, fake_site):
    with _pro(m, "lib2@test.com") as c:
        c.post("/api/scout", json={"platform": "lichess", "handle": "shared_guy", "max_games": 10})
    with _pro(m, "lib3@test.com") as c:
        r = c.post("/api/scout", json={"platform": "lichess", "handle": "Shared_Guy", "max_games": 10}).json()
        assert r["downloaded"] is False and len(fake_site) == 1  # the second user got the stored games
        assert [p["handle"] for p in c.get("/api/library").json()["players"]] == ["shared_guy"]


def test_notes_tags_search_explore_and_export(m, fake_site):
    with _pro(m, "lib4@test.com") as c:
        pid = c.post("/api/library", json={"platform": "lichess", "handle": "inc_four", "max_games": 40}).json()["player"]["id"]
        p = c.patch(f"/api/library/{pid}", json={"notes": "Plays the London", "tags": ["Club", "round 3", "club"]}).json()["player"]
        assert p["notes"] == "Plays the London" and p["tags"] == ["club", "round 3"]
        assert c.get("/api/library").json()["tags"] == ["club", "round 3"]
        # Games search with filters
        all_ = c.post("/api/library/games", json={"players": [pid]}).json()
        assert all_["total"] == 40 and all_["games"][0]["opponent"]
        white = c.post("/api/library/games", json={"players": [pid], "color": "white"}).json()
        black = c.post("/api/library/games", json={"players": [pid], "color": "black"}).json()
        assert white["total"] + black["total"] == 40 and all(g["color"] == "white" for g in white["games"])
        wins = c.post("/api/library/games", json={"players": [pid], "result": "win"}).json()
        assert all(g["score"] == 1 for g in wins["games"])
        assert c.post("/api/library/games", json={"players": [pid], "date_from": "2999-01-01"}).json()["total"] == 0
        assert c.post("/api/library/games", json={"players": [pid], "date_from": "bad"}).status_code == 422
        # Explorer from the start, then one move in, then by FEN
        ex = c.post("/api/library/explore", json={"players": [pid]}).json()
        assert ex["games"] == 40 and ex["moves"] and ex["list"]
        mv = ex["moves"][0]
        ex2 = c.post("/api/library/explore", json={"players": [pid], "moves": [mv["san"]]}).json()
        assert ex2["games"] == mv["games"] and ex2["turn"] == "black"
        ex3 = c.post("/api/library/explore", json={"players": [pid], "fen": ex2["fen"]}).json()
        assert ex3["games"] == ex2["games"]
        assert c.post("/api/library/explore", json={"players": [pid], "moves": ["e2e5"]}).status_code == 400
        # A game in the viewer
        g = c.get(f"/api/library/games/{ex['list'][0]['id']}").json()
        assert g["library"] and g["moves_san"] and len(g["fens"]) == len(g["moves_san"]) + 1 and "[White " in g["pgn"]
        # PGN export for ChessBase: valid, one game per stored game
        r = c.get(f"/api/library/export.pgn?players={pid}&color=white")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        stream, n = io.StringIO(r.text), 0
        while chess.pgn.read_game(stream):
            n += 1
        assert n == white["total"]


def test_other_users_cannot_see_your_library(m, fake_site):
    with _pro(m, "lib5@test.com") as c:
        pid = c.post("/api/library", json={"platform": "lichess", "handle": "priv_five", "max_games": 10}).json()["player"]["id"]
        gid = c.post("/api/library/games", json={"players": [pid]}).json()["games"][0]["id"]
    with _pro(m, "lib6@test.com") as c:
        assert c.get(f"/api/library/{pid}/report").status_code == 404
        assert c.get(f"/api/library/games/{gid}").status_code == 404
        assert c.post("/api/library/games", json={"players": [pid]}).json()["total"] == 0  # ids you don't hold are ignored
        assert c.post("/api/library/explore", json={"players": [pid]}).json()["games"] == 0
        assert c.get(f"/api/library/export.pgn?players={pid}").text == ""
        assert c.delete(f"/api/library/{pid}").status_code == 404


def test_pgn_upload_is_private_and_deleted_with_the_account(m):
    with _pro(m, "lib7@test.com") as c:
        uid = m.store.user_by_email("lib7@test.com")["id"]
        files = {"file": ("otb.pgn", DEMO.encode(), "application/x-chess-pgn")}
        choose = c.post("/api/library/upload", files=files).json()
        assert any(x["name"] == "demo_player" for x in choose["choose"])
        r = c.post("/api/library/upload", files=files, data={"name": "demo_player"}).json()
        assert r["new_games"] > 0 and r["player"]["platform"] == "pgn"
        pid = r["player"]["id"]
        assert c.post(f"/api/library/{pid}/update").status_code == 400  # nothing to download for a PGN player
        assert m.library.player(pid)["owner_id"] == uid
    with _pro(m, "lib8@test.com") as c:
        assert c.get(f"/api/library/{pid}/report").status_code == 404
    with client(m, "lib7@test.com", signup=False) as c:
        export = c.get("/api/me/export")
        assert export.status_code == 200
        assert c.request("DELETE", "/api/me", json={"password": "password123"}).json()["ok"]
    assert m.library.player(pid) is None  # private uploads go with the account


def test_removing_a_player(m, fake_site):
    with _pro(m, "lib9@test.com") as c:
        pid = c.post("/api/library", json={"platform": "lichess", "handle": "rm_nine", "max_games": 10}).json()["player"]["id"]
        assert c.delete(f"/api/library/{pid}").json()["ok"]
        assert c.get("/api/library").json()["players"] == []
        assert m.library.player(pid) is not None  # public games stay cached for other users


def test_library_size_limit(m, fake_site, monkeypatch):
    monkeypatch.setitem(m.PLANS["pro"], "library_players", 1)
    with _pro(m, "lib10@test.com") as c:
        assert c.post("/api/library", json={"platform": "lichess", "handle": "one", "max_games": 10}).status_code == 200
        r = c.post("/api/library", json={"platform": "lichess", "handle": "two", "max_games": 10})
        assert r.status_code == 402 and "holds 1" in r.json()["detail"]["message"]
        assert c.post("/api/library", json={"platform": "lichess", "handle": "one", "max_games": 10}).status_code == 200


def test_out_of_downloads_still_opens_stored_players(m, fake_site, monkeypatch):
    with _pro(m, "lib11@test.com") as c:
        c.post("/api/library", json={"platform": "lichess", "handle": "stored_one", "max_games": 10})
        monkeypatch.setitem(m.PLANS["pro"], "scouts_per_day", 1)
        pid = c.get("/api/library").json()["players"][0]["id"]
        _age(m, pid)
        r = c.post("/api/scout", json={"platform": "lichess", "handle": "stored_one", "max_games": 10})
        assert r.status_code == 200 and r.json()["downloaded"] is False and r.json()["games"] == 10
        assert c.post("/api/scout", json={"platform": "lichess", "handle": "brand_new", "max_games": 10}).status_code == 429
