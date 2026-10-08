"""v0.6: the new Train flow (opponent's move first, hints, practice retries, solutions, Explore), bigger
imports, and the Coach plan (roster, squad heatmap, invites that give students Pro, homework)."""

import chess
import pytest
from fastapi.testclient import TestClient

from app.games import parse_pgn
from app.plans import PLANS, effective_plan, is_pro
from tests.conftest import needs_engine
from tests.test_api import DEMO, client, demo_account


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


def _puzzles(c, a):
    return c.get(f"/api/accounts/{a['id']}/puzzles?due=true&limit=50").json()


# ---- Train ------------------------------------------------------------------------------------
@needs_engine
def test_puzzles_carry_the_opponents_move_and_hide_the_answer(m):
    with client(m, "train-ctx@test.com") as c:
        a = demo_account(c)
        rows = _puzzles(c, a)
        assert rows
        with_prev = [r for r in rows if r.get("prev_uci")]
        assert with_prev, "puzzles from games should know the move that led to them"
        r = with_prev[0]
        b = chess.Board(r["prev_fen"])
        b.push(chess.Move.from_uci(r["prev_uci"]))
        assert b.board_fen() == r["fen"].split()[0]  # playing the opponent's move reaches the puzzle
        assert r["opponent"] and "solution" not in r and "line" not in r and "accept" not in r


@needs_engine
def test_hint_reveal_and_practice_attempts(m):
    with client(m, "train-flow@test.com") as c:
        a = demo_account(c)
        p = _puzzles(c, a)[0]
        sol = m.store.get_puzzle(p["id"])["solution"]
        h1 = c.post(f"/api/puzzles/{p['id']}/hint", json={"level": 1}).json()
        assert h1 == {"from": sol[:2], "to": None}
        assert c.post(f"/api/puzzles/{p['id']}/hint", json={"level": 2}).json()["to"] == sol[2:4]
        # A correct answer after a hint still brings the puzzle back soon
        r = c.post(f"/api/puzzles/{p['id']}/attempt", json={"move": sol, "hinted": True}).json()
        assert r["verdict"] == "correct" and r["box"] == 1 and r["next_review_in_days"] < 0.1
        # Practice retries are graded but don't touch the schedule
        before = m.store.get_puzzle(p["id"])
        r = c.post(f"/api/puzzles/{p['id']}/attempt", json={"move": sol, "practice": True}).json()
        assert r["verdict"] == "correct" and r["practice"] is True and "box" not in r
        after = m.store.get_puzzle(p["id"])
        assert (after["box"], after["attempts"], after["due_at"]) == (before["box"], before["attempts"], before["due_at"])
        # Reveal: the line, counted as a miss unless practising
        q = _puzzles(c, a)
        other = next(x for x in q if x["id"] != p["id"])
        rv = c.post(f"/api/puzzles/{other['id']}/reveal", json={}).json()
        assert rv["solution"] and rv["line"] and rv["box"] == 1
        assert c.post(f"/api/puzzles/{other['id']}/reveal", json={"practice": True}).json()["practice"] is True
        # Someone else's puzzle is invisible
        with client(m, "train-other@test.com") as c2:
            assert c2.post(f"/api/puzzles/{p['id']}/hint", json={"level": 1}).status_code == 404
            assert c2.post(f"/api/puzzles/{p['id']}/reveal", json={}).status_code == 404


@needs_engine
def test_engine_analyse_for_explore(m):
    m.analyse_limiter._hits.clear()
    with client(m, "explore@test.com") as c:
        r = c.post("/api/engine/analyse", json={"fen": "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"}).json()
        assert r["mate"] == 1 and r["best"] == "d1d8" and r["best_san"] == "Rd8#" and r["line"][0] == "Rd8#"
        over = c.post("/api/engine/analyse", json={"fen": "3R2k1/5ppp/8/8/8/8/5PPP/6K1 b - - 1 1"}).json()
        assert over["over"] is True and over["result"] == "1-0"
        assert c.post("/api/engine/analyse", json={"fen": "not a fen"}).status_code == 400
        assert c.post("/api/engine/analyse", json={"fen": "8/8/8/8/8/8/8/8 w - - 0 1"}).status_code == 400  # no kings


def test_bigger_imports_on_every_plan():
    assert PLANS["free"]["max_games_per_sync"] == 200
    assert PLANS["pro"]["max_games_per_sync"] == 500
    assert PLANS["coach"]["max_games_per_sync"] == 500


def test_games_are_cached_until_they_change(tmp_path):
    from app.store import Store

    st = Store(str(tmp_path / "c.db"))
    assert st.load_games("k") == []
    st._games_cache["k"] = (("stale",), ["x"])  # a different signature: must be ignored
    assert st.load_games("k") == []


# ---- Coach plan ---------------------------------------------------------------------------------
def _coach(m, email):
    c = client(m, email)
    m.store.set_plan(m.store.user_by_email(email)["id"], "coach")
    return c


def test_coach_plan_entitlements():
    assert is_pro({"plan": "coach"}) and effective_plan({"plan": "coach"}) == "coach"
    assert "students" in PLANS["coach"]["features"] and PLANS["coach"]["max_students"] == 20
    assert "students" not in PLANS["pro"]["features"]


def test_dashboard_is_for_coaches_only(m):
    with client(m, "not-a-coach@test.com") as c:
        r = c.get("/api/coach/students")
        assert r.status_code == 402 and r.json()["detail"]["upgrade"] is True
        assert c.post("/api/coach/students", json={"name": "A", "platform": "lichess", "handle": "abc"}).status_code == 402


@needs_engine
def test_coach_roster_heatmap_invite_and_homework(m, monkeypatch):
    monkeypatch.setattr("app.main.check_account", lambda platform, name: name)
    monkeypatch.setattr("app.sync.fetch_games", lambda platform, handle, n, classes, since=None: parse_pgn(DEMO, "demo_player")[:n])
    with _coach(m, "coach1@test.com") as coach:
        assert coach.get("/api/me").json()["plan"] == "coach"
        s = coach.post("/api/coach/students", json={"name": "Riya", "platform": "lichess", "handle": "demo_player"}).json()
        assert s["name"] == "Riya" and s["handle"] == "demo_player"
        assert coach.post("/api/coach/students", json={"name": "Again", "platform": "lichess", "handle": "demo_player"}).status_code == 409
        board = coach.get("/api/coach/students").json()
        row = board["students"][0]
        assert row["games"] > 0 and row["base_rating"] and row["skills"] and board["max_students"] == 20
        assert all({"offset", "rating", "label"} <= set(v) for v in row["skills"].values())
        one = coach.get(f"/api/coach/students/{s['id']}").json()
        assert one["dna"]["skills"] and one["recent_games"] and one["assignments"] == []
        # The student's account is the coach's, but doesn't count against the coach's own linked accounts
        me = coach.get("/api/me").json()
        assert [a["role"] for a in me["accounts"]] == ["student"]
        # Coach can open the student's games like their own
        gid = one["recent_games"][0]["game_id"]
        assert coach.get(f"/api/accounts/{s['account_id']}/game/{gid}").status_code == 200

        # Homework
        hw = coach.post(f"/api/coach/students/{s['id']}/assignments", json={"title": "15 tactics puzzles", "skill": "tactics", "due_days": 7}).json()
        assert hw["title"] == "15 tactics puzzles" and hw["due_at"]
        assert coach.post(f"/api/coach/students/{s['id']}/assignments", json={"title": "x", "skill": "juggling"}).status_code == 400
        coach.patch(f"/api/coach/students/{s['id']}", json={"note": "Works on endgames on Fridays"})
        assert m.store.student(s["id"])["note"] == "Works on endgames on Fridays"

        # Invite: the student joins and gets Pro, sees homework, can tick it but not delete it
        inv = coach.post(f"/api/coach/students/{s['id']}/invite", json={"email": "riya@test.com"}).json()
        assert "/join#coach=" in inv["link"] and inv["sent_to"] == "riya@test.com"
        token = inv["link"].split("#coach=")[1]
        assert coach.post("/api/coach/join", json={"token": token}).status_code == 400  # not your own invite
        token = coach.post(f"/api/coach/students/{s['id']}/invite", json={}).json()["link"].split("#coach=")[1]
        with client(m, "riya@test.com") as st:
            assert st.get("/api/me").json()["plan"] == "free"
            assert st.post("/api/coach/join", json={"token": token}).json()["ok"]
            assert st.post("/api/coach/join", json={"token": token}).status_code == 400  # single use
            me = st.get("/api/me").json()
            assert me["plan"] == "pro" and me["sponsored_by"] == "coach1@test.com" and me["coaches"][0]["student_id"] == s["id"]
            mine = st.get("/api/me/coaching").json()
            assert mine[0]["assignments"][0]["title"] == "15 tactics puzzles"
            assert st.patch(f"/api/assignments/{hw['id']}", json={"done": True}).json()["done_at"]
            assert st.delete(f"/api/assignments/{hw['id']}").status_code == 403
            assert st.get("/api/coach/students").status_code == 402  # Pro, but not a coach
            assert coach.get("/api/coach/students").json()["students"][0]["homework_done"] == 1
            assert coach.get(f"/api/coach/students/{s['id']}").json()["joined"] is True
            # Pro lasts only while the coach is on the Coach plan
            m.store.set_plan(m.store.user_by_email("coach1@test.com")["id"], "free")
            assert st.get("/api/me").json()["plan"] == "free"
            m.store.set_plan(m.store.user_by_email("coach1@test.com")["id"], "coach")
            assert st.delete(f"/api/me/coaching/{s['id']}").json()["ok"]
            assert st.get("/api/me").json()["plan"] == "free"

        # Strangers can't see or touch this roster
        with _coach(m, "coach2@test.com") as other:
            assert other.get(f"/api/coach/students/{s['id']}").status_code == 404
            assert other.patch(f"/api/assignments/{hw['id']}", json={"done": False}).status_code == 404

        # Removing a student deletes their analysis and homework
        acct_key = m.player_key(m.store.account(s["account_id"]))
        assert coach.delete(f"/api/coach/students/{s['id']}").json()["ok"]
        assert m.store.student(s["id"]) is None and m.store.account(s["account_id"]) is None
        assert m.store.count_games(acct_key) == 0 and m.store.assignment(hw["id"]) is None


def test_roster_is_capped(m, monkeypatch):
    monkeypatch.setattr("app.main.check_account", lambda platform, name: name)
    monkeypatch.setitem(PLANS["coach"], "max_students", 1)
    with _coach(m, "coach-cap@test.com") as c:
        assert c.post("/api/coach/students", json={"name": "One", "platform": "pgn", "handle": "Player One"}).status_code == 200
        r = c.post("/api/coach/students", json={"name": "Two", "platform": "pgn", "handle": "Player Two"})
        assert r.status_code == 402 and "room for 1" in r.json()["detail"]["message"]


def test_coach_join_page_and_admin_grant(m, capsys):
    import importlib.util
    from pathlib import Path

    assert TestClient(m.app).get("/join").status_code == 200
    client(m, "grant-coach@test.com")
    spec = importlib.util.spec_from_file_location("admin_coach", Path(__file__).resolve().parent.parent / "scripts" / "admin.py")
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    assert admin.main(["grant", "grant-coach@test.com", "--plan", "coach"]) == 0
    assert "now on Coach" in capsys.readouterr().out
    assert effective_plan(m.store.user_by_email("grant-coach@test.com")) == "coach"
    assert admin.main(["grant", "grant-coach@test.com", "--plan", "gold"]) == 1
