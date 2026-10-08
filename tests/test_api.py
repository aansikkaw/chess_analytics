"""API: accounts, sessions, data isolation, plan gates, daily limits, imports, scout, auto-sync."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.games import parse_pgn
from tests.conftest import needs_engine

DEMO = (Path(__file__).resolve().parent.parent / "sample_data" / "demo_games.pgn").read_text()


@pytest.fixture(scope="module")
def app_mod():
    import app.main as m

    return m


def client(app_mod, email, password="password123", signup=True):
    """Signed-in test client. Falls back to the other auth route so tests don't depend on run order."""
    c = TestClient(app_mod.app)
    c.__enter__()
    first, second = ("signup", "login") if signup else ("login", "signup")
    r = c.post(f"/api/auth/{first}", json={"email": email, "password": password})
    if r.status_code != 200:
        r = c.post(f"/api/auth/{second}", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def demo_account(c):
    a = c.post("/api/accounts", json={"platform": "demo"}).json()
    job = c.post(f"/api/accounts/{a['id']}/pgn", json={"pgn": "demo"}).json()
    assert c.get(f"/api/jobs/{job['job_id']}").json()["state"] == "done"
    return a


# ---- auth ----------------------------------------------------------------------------
def test_signup_login_logout_flow(app_mod):
    c = TestClient(app_mod.app)
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/signup", json={"email": "bad", "password": "password123"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "u1@test.com", "password": "short"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "U1@Test.com", "password": "password123"}).status_code == 200
    assert c.get("/api/me").json()["email"] == "u1@test.com"  # emails are normalised
    assert c.post("/api/auth/signup", json={"email": "u1@test.com", "password": "password123"}).status_code == 409
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/login", json={"email": "u1@test.com", "password": "wrongpass1"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "u1@test.com", "password": "password123"}).status_code == 200


def test_passwords_are_hashed(app_mod):
    client(app_mod, "hash@test.com")
    u = app_mod.store.user_by_email("hash@test.com")
    assert u["pw_hash"].startswith("scrypt$") and "password123" not in u["pw_hash"]


def test_login_rate_limit(app_mod):
    app_mod.login_limiter._hits.clear()
    c = TestClient(app_mod.app)
    for _ in range(10):
        c.post("/api/auth/login", json={"email": "nobody@test.com", "password": "whatever12"})
    assert c.post("/api/auth/login", json={"email": "nobody@test.com", "password": "whatever12"}).status_code == 429
    app_mod.login_limiter._hits.clear()


def test_admin_email_gets_pro(app_mod):
    me = client(app_mod, "admin@test.com").get("/api/me").json()
    assert me["plan"] == "pro" and me["is_admin"]


# ---- accounts, isolation, gates ------------------------------------------------------------
@needs_engine
def test_isolation_between_users(app_mod):
    a_client = client(app_mod, "iso-a@test.com")
    b_client = client(app_mod, "iso-b@test.com")
    acct = demo_account(a_client)
    assert a_client.get(f"/api/accounts/{acct['id']}/profile").status_code == 200
    assert b_client.get(f"/api/accounts/{acct['id']}/profile").status_code == 404
    assert b_client.delete(f"/api/accounts/{acct['id']}").status_code == 404
    pz = a_client.get(f"/api/accounts/{acct['id']}/puzzles").json()[0]
    assert "solution" not in pz
    assert b_client.post(f"/api/puzzles/{pz['id']}/attempt", json={"move": "e2e4"}).status_code == 404
    # Same demo handle linked by B gets B's own, separate data.
    b_acct = demo_account(b_client)
    assert b_acct["id"] != acct["id"]


@needs_engine
def test_free_plan_gates_and_limits(app_mod, monkeypatch):
    c = client(app_mod, "free@test.com")
    acct = demo_account(c)
    for path in ("repertoire", "progress"):
        r = c.get(f"/api/accounts/{acct['id']}/{path}")
        assert r.status_code == 402 and r.json()["detail"]["upgrade"] is True
    assert c.post("/api/scout", json={"platform": "lichess", "handle": "bob"}).status_code == 402
    assert c.patch(f"/api/accounts/{acct['id']}", json={"auto_sync": True}).status_code == 402
    # Coach: 15 a day on Free
    monkeypatch.setitem(app_mod.PLANS["free"], "coach_messages_per_day", 2)
    for _ in range(2):
        assert c.post(f"/api/accounts/{acct['id']}/coach", json={"message": "study plan?"}).status_code == 200
    r = c.post(f"/api/accounts/{acct['id']}/coach", json={"message": "again"})
    assert r.status_code == 429 and r.json()["detail"]["upgrade"] is True
    # Account cap
    c.post("/api/accounts", json={"platform": "pgn", "handle": "otb_me"})
    assert c.post("/api/accounts", json={"platform": "pgn", "handle": "third"}).status_code == 402


@needs_engine
def test_coach_reply_has_citations(app_mod):
    c = client(app_mod, "coach@test.com")
    acct = demo_account(c)
    r = c.post(f"/api/accounts/{acct['id']}/coach", json={"message": "show me my worst blunder"}).json()
    assert r["mode"] == "offline" and r["citations"]
    card = next(iter(r["citations"].values()))
    assert card["fen"] and card["you_played"]


@needs_engine
def test_pro_endpoints(app_mod):
    c = client(app_mod, "admin@test.com", signup=False)
    acct = demo_account(c)
    rep = c.get(f"/api/accounts/{acct['id']}/repertoire").json()
    assert {"white", "black", "leaks", "recurring_mistakes"} <= rep.keys()
    prog = c.get(f"/api/accounts/{acct['id']}/progress").json()
    assert "buckets" in prog and (prog["buckets"] or prog["note"])  # with 3 test games there may be too few per period
    assert c.patch(f"/api/accounts/{acct['id']}", json={"auto_sync": True}).status_code == 400  # demo can't auto-sync


# ---- live sources (mocked) ----------------------------------------------------------------
@needs_engine
def test_link_and_sync_lichess_account(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "check_account", lambda platform, handle: "DemoPlayer")
    monkeypatch.setattr("app.sync.fetch_games", lambda platform, handle, n, classes, since: parse_pgn(DEMO, "demo_player")[:n])
    c = client(app_mod, "sync@test.com")
    a = c.post("/api/accounts", json={"platform": "lichess", "handle": "demoplayer"}).json()
    assert a["handle"] == "DemoPlayer"
    job = c.post(f"/api/accounts/{a['id']}/sync", json={"time_classes": ["rapid"], "max_games": 2}).json()
    j = c.get(f"/api/jobs/{job['job_id']}").json()
    assert j["state"] == "done" and j["new_games"] == 2
    me = c.get("/api/me").json()
    assert me["accounts"][0]["games"] == 2 and me["accounts"][0]["time_classes"] == ["rapid"]
    # Second sync: nothing new
    job2 = c.post(f"/api/accounts/{a['id']}/sync", json={"max_games": 2}).json()
    assert c.get(f"/api/jobs/{job2['job_id']}").json()["new_games"] == 0


def test_link_unknown_lichess_user_is_a_clear_error(app_mod, monkeypatch):
    from app.games import GameImportError

    def nope(platform, handle):
        raise GameImportError(f"No Lichess account called '{handle}'.")

    monkeypatch.setattr(app_mod, "check_account", nope)
    c = client(app_mod, "nouser@test.com")
    r = c.post("/api/accounts", json={"platform": "lichess", "handle": "ghost_user"})
    assert r.status_code == 400 and "No Lichess account" in r.json()["detail"]


@needs_engine
def test_scout_endpoint(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "check_account", lambda platform, handle: handle)
    monkeypatch.setattr(app_mod, "fetch_games", lambda platform, handle, n, classes: parse_pgn(DEMO, "demo_player")[:10])
    c = client(app_mod, "admin@test.com", signup=False)
    r = c.post("/api/scout", json={"platform": "lichess", "handle": "demo_player"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["games"] == 10 and body["insights"]


@needs_engine
def test_failed_scout_does_not_use_up_the_daily_limit(app_mod, monkeypatch):
    from app.games import GameImportError

    def down(*a):
        raise GameImportError("Couldn't reach Chess.com (the connection timed out, even after retrying).")
    monkeypatch.setattr(app_mod, "check_account", down)
    c = client(app_mod, "admin@test.com", signup=False)
    before = c.get("/api/me").json()["usage_today"]["scout"]
    r = c.post("/api/scout", json={"platform": "chesscom", "handle": "carol"})
    assert r.status_code == 400 and "timed out" in r.json()["detail"]
    assert c.get("/api/me").json()["usage_today"]["scout"] == before


@needs_engine
def test_autosync_only_for_pro_accounts(app_mod, monkeypatch):
    calls = []
    monkeypatch.setattr("app.sync.fetch_games", lambda platform, handle, n, classes, since: calls.append(handle) or [])
    pro_c = client(app_mod, "admin@test.com", signup=False)
    monkeypatch.setattr(app_mod, "check_account", lambda platform, handle: handle)
    a = pro_c.post("/api/accounts", json={"platform": "chesscom", "handle": "auto_pro"}).json()
    assert pro_c.patch(f"/api/accounts/{a['id']}", json={"auto_sync": True}).status_code == 200
    # A free user's account flagged in the DB directly must still be skipped.
    free_c = client(app_mod, "autofree@test.com")
    b = free_c.post("/api/accounts", json={"platform": "chesscom", "handle": "auto_free"}).json()
    app_mod.store.update_account(b["id"], auto_sync=1)
    from app.sync import AutoSync

    AutoSync(app_mod.jobs, app_mod.store, app_mod.settings, 60).sync_all()
    assert "auto_pro" in calls and "auto_free" not in calls


def test_upgrade_request_and_plans(app_mod):
    c = client(app_mod, "wants-pro@test.com")
    assert {p["id"] for p in c.get("/api/plans").json()} == {"free", "pro", "event", "coach"}
    assert c.post("/api/upgrade-request", json={"note": "please"}).json()["ok"]
    assert any(r["email"] == "wants-pro@test.com" for r in app_mod.store.upgrade_requests())


def test_health_and_index(app_mod):
    with TestClient(app_mod.app) as c:
        assert c.get("/api/health").json()["coach_mode"] == "offline"
        assert c.get("/").status_code == 200
        assert c.get("/api/health").headers["x-content-type-options"] == "nosniff"


def test_chesscom_can_be_switched_off(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "settings", app_mod.settings.__class__(**{**app_mod.settings.__dict__, "chesscom_enabled": False}))
    c = client(app_mod, "nocc@test.com")
    r = c.post("/api/accounts", json={"platform": "chesscom", "handle": "someone"})
    assert r.status_code == 400 and "Chess.com" in r.json()["detail"]
    assert c.get("/api/health").json()["platforms"] == ["lichess"]


# ---- v0.4: ChessBase upload, Prep Check, OKF knowledge ---------------------------------------------
from tests.test_prep import GAMES, WHITE_REP  # noqa: E402


def _pgn_account(c, name="Aanya Sikka"):
    return c.post("/api/accounts", json={"platform": "pgn", "handle": name.replace(" ", "_")}).json()


@needs_engine
def test_chessbase_upload_flow(app_mod):
    c = client(app_mod, "cb-free@test.com")
    assert c.post("/api/accounts", json={"platform": "pgn", "handle": "x"}).status_code == 400  # too short
    wrong = c.post("/api/accounts", json={"platform": "pgn", "handle": "Magnus Carlsen"}).json()
    r = c.post(f"/api/accounts/{wrong['id']}/upload", files={"file": ("games.pgn", GAMES.encode("cp1252"))})
    assert r.status_code == 400 and "Sikka, Aanya" in r.json()["detail"]["names"]  # tells you which names are in the file
    c.delete(f"/api/accounts/{wrong['id']}")
    a = c.post("/api/accounts", json={"platform": "pgn", "handle": "Aanya Sikka"}).json()  # "First Last" matches "Last, First"
    assert a["handle"] == "Aanya Sikka"
    job = c.post(f"/api/accounts/{a['id']}/upload", files={"file": ("cb.pgn", GAMES.encode("cp1252"))}).json()
    j = c.get(f"/api/jobs/{job['job_id']}").json()
    assert j["state"] == "done" and j["new_games"] == 3  # MAX_GAMES=3 in tests caps each import
    assert c.get(f"/api/accounts/{a['id']}/profile").json()["dna"]["games"] == 3
    bad = c.post(f"/api/accounts/{a['id']}/upload", files={"file": ("db.cbh", b"\x00")})
    assert bad.status_code == 400 and "File > New > Database" in bad.json()["detail"]


@needs_engine
def test_upload_prep_check_and_knowledge(app_mod):
    c = client(app_mod, "admin@test.com", signup=False)
    acct = c.post("/api/accounts", json={"platform": "pgn", "handle": "Sikka, Aanya"}).json()
    job = c.post(f"/api/accounts/{acct['id']}/upload", files={"file": ("cb.pgn", GAMES.encode("cp1252"))}).json()
    assert c.get(f"/api/jobs/{job['job_id']}").json()["state"] == "done"

    # Knowledge bundle exists and is conformant
    k = c.get(f"/api/accounts/{acct['id']}/knowledge").json()
    assert k["concepts"] > 10 and k["conformance_problems"] == [] and "Player Profile" in k["types"]
    pc = c.get(f"/api/accounts/{acct['id']}/concept", params={"id": "/player"}).json()
    assert pc["type"] == "Player Profile" and pc["markdown"].startswith("---\ntype: Player Profile")
    assert c.get(f"/api/accounts/{acct['id']}/concept", params={"id": "/nope"}).status_code == 404

    # Prep Check
    r = c.post(f"/api/accounts/{acct['id']}/repertoires", files={"file": ("e4.pgn", WHITE_REP.encode())}, data={"color": "auto", "name": "My e4"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["color"] == "white"
    assert c.get(f"/api/jobs/{body['job_id']}").json()["state"] == "done"
    reps = c.get(f"/api/accounts/{acct['id']}/repertoires").json()
    rep = reps[0]["report"]
    assert rep["deviations"][0]["played"] == "Nc3" and "pgn" not in reps[0]

    # The repertoire now shows up in the bundle, and drills are in the puzzle deck
    k = c.get(f"/api/accounts/{acct['id']}/knowledge").json()
    assert "Repertoire Check" in k["types"]
    drills = [p for p in c.get(f"/api/accounts/{acct['id']}/puzzles?limit=100").json() if p["kind"] == "prep"]
    assert drills and drills[0]["played_san"] == "Nc3"  # positions you got wrong come first
    wrong = c.post(f"/api/puzzles/{drills[0]['id']}/attempt", json={"move": "b1c3"}).json()
    assert wrong["verdict"] == "wrong" and "d4" in wrong["note"]
    right = c.post(f"/api/puzzles/{drills[0]['id']}/attempt", json={"move": "d4"}).json()
    assert right["verdict"] == "correct"

    # Export: a zip containing the player bundle and the principles bundle
    z = c.get(f"/api/accounts/{acct['id']}/bundle.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    import io as _io
    import zipfile as _zf

    names = _zf.ZipFile(_io.BytesIO(z.content)).namelist()
    assert "index.md" in names and "player.md" in names and "knowledge/principles/clock-management.md" in names
    assert any(n.startswith("prep/") for n in names)

    # Coach can find the prep concept
    ans = c.post(f"/api/accounts/{acct['id']}/coach", json={"message": "Where do I forget my prep?"}).json()
    assert "/prep/" in ans["reply"]

    # Deleting the repertoire removes its drills and its concept
    assert c.delete(f"/api/repertoires/{reps[0]['id']}").json()["ok"]
    assert not [p for p in c.get(f"/api/accounts/{acct['id']}/puzzles?limit=100").json() if p["kind"] == "prep"]
    assert "Repertoire Check" not in c.get(f"/api/accounts/{acct['id']}/knowledge").json()["types"]


@needs_engine
def test_prep_and_pro_concepts_gated_for_free(app_mod):
    c = client(app_mod, "gate@test.com")
    a = demo_account(c)
    assert c.post(f"/api/accounts/{a['id']}/repertoires", files={"file": ("e4.pgn", WHITE_REP.encode())}).status_code == 402
    k = c.get(f"/api/accounts/{a['id']}/knowledge").json()
    assert "Progress Report" not in k["types"] and k["hidden_pro_concepts"] > 0
    assert c.get(f"/api/accounts/{a['id']}/concept", params={"id": "/progress"}).status_code == 404
    import io as _io
    import zipfile as _zf

    names = _zf.ZipFile(_io.BytesIO(c.get(f"/api/accounts/{a['id']}/bundle.zip").content)).namelist()
    assert "player.md" in names and not any(n.startswith(("progress", "openings/", "recurring")) for n in names)


def test_migration_from_older_database(tmp_path):
    import sqlite3

    from app.store import Store

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE games (username TEXT, game_id TEXT, data TEXT, PRIMARY KEY (username, game_id));
        CREATE TABLE puzzles (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, game_id TEXT, ply INTEGER, fen TEXT,
            solution TEXT, solution_san TEXT, line TEXT, played_san TEXT, themes TEXT, phase TEXT, win_loss REAL,
            box INTEGER DEFAULT 1, due_at REAL, attempts INTEGER DEFAULT 0, solved INTEGER DEFAULT 0, UNIQUE (username, game_id, ply));
        INSERT INTO puzzles (username, game_id, ply, fen, solution, solution_san, line, played_san, themes, phase, win_loss, due_at)
            VALUES ('u', 'g', 1, 'x', 'e2e4', 'e4', '[]', 'd4', '[]', 'opening', 10, 0);
    """)
    con.commit()
    con.close()
    st = Store(str(db))
    p = st.puzzles("u")[0]
    assert p["accept"] == [] and p["kind"] == "mistake"
    assert st.repertoires(1) == []
