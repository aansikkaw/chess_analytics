"""v0.7 production hardening: cross-site write blocking, request IDs, shared rate limits, kill switches,
the per-user AI token budget, the audit log, consent at sign-up and on coach rosters, data export, legal pages."""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import switches
from app.coach import CoachReply
from tests.conftest import needs_engine
from tests.test_api import client, demo_account

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


@pytest.fixture(autouse=True)
def _all_switches_on(m):
    yield
    for name in switches.SWITCHES:
        m.store.kv_set(f"switch:{name}", {"on": True})
    switches.clear_cache()


def _switch(m, name, on):
    switches.set_switch(m.store, name, on, actor="test")


# ---- CSRF: writes from other websites -----------------------------------------------------------
def test_cross_site_writes_are_refused(m):
    c = TestClient(m.app)
    body = {"email": "x@test.com", "password": "password123"}
    r = c.post("/api/auth/login", json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "other websites" in r.json()["detail"]
    assert c.post("/api/auth/login", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    # Our own page (same host) and non-browser clients (no Origin) get through to the real check
    assert c.post("/api/auth/login", json=body, headers={"Origin": "http://testserver"}).status_code == 401
    assert c.post("/api/auth/login", json=body).status_code == 401
    # Reads are never blocked
    assert c.get("/api/config", headers={"Origin": "https://evil.example"}).status_code == 200


def test_every_response_has_a_request_id(m):
    c = TestClient(m.app)
    assert len(c.get("/api/config").headers["X-Request-ID"]) >= 8
    assert c.get("/api/config", headers={"X-Request-ID": "abc123"}).headers["X-Request-ID"] == "abc123"


def test_unhandled_errors_quote_the_request_id(m, monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(m, "public_plans", boom)
    r = TestClient(m.app, raise_server_exceptions=False).get("/api/plans")
    assert r.status_code == 500 and r.json()["request_id"] in r.json()["detail"] and "kaboom" not in r.text


# ---- rate limits shared between processes ------------------------------------------------------
def test_rate_limits_are_shared_between_processes(tmp_path):
    from app.auth import SharedLimiter
    from app.store import Store

    st = Store(str(tmp_path / "rl.db"))
    web1, web2 = SharedLimiter(st, "login", limit=3), SharedLimiter(Store(str(tmp_path / "rl.db")), "login", limit=3)
    for _ in range(2):
        web1.fail("1.2.3.4")
    web2.fail("1.2.3.4")
    assert web1.blocked("1.2.3.4") and web2.blocked("1.2.3.4")  # each saw only some of the 3 hits
    assert not web1.blocked("5.6.7.8")
    web2.reset("1.2.3.4")
    assert not web1.blocked("1.2.3.4")


# ---- kill switches -----------------------------------------------------------------------------
def test_switches_default_on_and_pause_signups(m):
    assert switches.paused(m.store) == []
    _switch(m, "signups", False)
    r = TestClient(m.app).post("/api/auth/signup", json={"email": "paused@test.com", "password": "password123", "accept_terms": True})
    assert r.status_code == 503 and "paused" in r.json()["detail"]
    status = TestClient(m.app).get("/api/status?external=false").json()
    assert status["status"] != "ok" and [p["name"] for p in status["paused"]] == ["signups"]


def test_switch_imports_off_blocks_syncing(m, monkeypatch):
    monkeypatch.setattr("app.main.check_account", lambda platform, name: name)
    with client(m, "switch-sync@test.com") as c:
        a = c.post("/api/accounts", json={"platform": "lichess", "handle": "someone"})
        assert a.status_code == 200, a.text
        _switch(m, "imports", False)
        r = c.post(f"/api/accounts/{a.json()['id']}/sync", json={})
        assert r.status_code == 503 and "paused" in r.json()["detail"]


def test_only_the_owner_can_flip_switches_and_it_is_audited(m):
    with client(m, "not-owner@test.com") as c:
        assert c.get("/api/admin/switches").status_code == 403
        assert c.post("/api/admin/switches/ai_coach", json={"on": False}).status_code == 403
    with client(m, "admin@test.com", signup=False) as c:
        rows = c.get("/api/admin/switches").json()
        assert {r["name"] for r in rows} == set(switches.SWITCHES) and all(r["on"] for r in rows)
        assert c.post("/api/admin/switches/nope", json={"on": False}).status_code == 404
        assert c.post("/api/admin/switches/ai_coach", json={"on": False}).json()["on"] is False
        assert not switches.is_on(m.store, "ai_coach")
        log = c.get("/api/admin/audit?limit=5").json()
        assert log[0]["action"] == "switch_off" and log[0]["target"] == "ai_coach" and log[0]["actor"] == "admin@test.com"


def test_emails_switch_saves_to_outbox(m, tmp_path):
    from app.mailer import Mailer

    import dataclasses

    s = dataclasses.replace(m.settings, brevo_api_key="key", smtp_from="a@b.co")
    mailer = Mailer(s, m.store)
    mailer.outbox = tmp_path
    _switch(m, "emails", False)
    assert mailer.send("x@test.com", "Hi", "text")
    assert list(tmp_path.glob("*.eml"))


# ---- AI coach: pause and daily token budget ----------------------------------------------------
class _FakeCoach:
    seen: list = []

    def __init__(self, handle, tools, backends, **kw):
        self.backends = backends

    def chat(self, message, history=None):
        _FakeCoach.seen.append(len(self.backends))
        if self.backends:
            return CoachReply(reply="ai", mode="agent", tokens=5000, model="fake")
        return CoachReply(reply="built-in", mode="offline")


@needs_engine
def test_ai_coach_pause_and_token_budget(m, monkeypatch):
    from app.llm import Backend

    monkeypatch.setattr(m, "Coach", _FakeCoach)
    monkeypatch.setattr(m, "resolve_backends", lambda s: [Backend("openai", "fake-model", "https://x", "k")])
    _FakeCoach.seen = []
    with client(m, "budget@test.com") as c:
        uid = m.store.user_by_email("budget@test.com")["id"]
        m.store.update_user(uid, email_verified_at=1)
        a = demo_account(c)
        r = c.post(f"/api/accounts/{a['id']}/coach", json={"message": "hi"}).json()
        assert r["mode"] == "agent" and m.store.usage_today(uid, "ai_tokens") == 5000  # tokens are counted
        _switch(m, "ai_coach", False)
        r = c.post(f"/api/accounts/{a['id']}/coach", json={"message": "hi"}).json()
        assert r["mode"] == "offline" and "paused" in r["notice"]
        _switch(m, "ai_coach", True)
        m.store.bump_usage(uid, "ai_tokens", m.PLANS["free"]["ai_tokens_per_day"])
        r = c.post(f"/api/accounts/{a['id']}/coach", json={"message": "hi"}).json()
        assert r["mode"] == "offline" and "allowance" in r["notice"]
    assert _FakeCoach.seen == [1, 0, 0]


def test_prompt_treats_game_text_as_data():
    from app.coach import SYSTEM_PROMPT

    assert "never instructions" in SYSTEM_PROMPT


# ---- consent ------------------------------------------------------------------------------------
def test_signup_needs_terms_and_records_them(m):
    c = TestClient(m.app)
    r = c.post("/api/auth/signup", json={"email": "noterms@test.com", "password": "password123"})
    assert r.status_code == 400 and "18" in r.json()["detail"]
    assert c.post("/api/auth/signup", json={"email": "terms@test.com", "password": "password123", "accept_terms": True}).status_code == 200
    assert m.store.user_by_email("terms@test.com")["terms_accepted_at"]
    assert c.get("/api/config").json()["require_terms"] is True


def test_roster_needs_student_consent(m, monkeypatch):
    monkeypatch.setattr("app.main.check_account", lambda platform, name: name)
    with client(m, "consent-coach@test.com") as c:
        m.store.set_plan(m.store.user_by_email("consent-coach@test.com")["id"], "coach")
        r = c.post("/api/coach/students", json={"name": "Kid", "platform": "pgn", "handle": "Kid Player"})
        assert r.status_code == 400 and "agreed" in r.json()["detail"]
        s = c.post("/api/coach/students", json={"name": "Kid", "platform": "pgn", "handle": "Kid Player", "consent": True}).json()
        assert m.store.student(s["id"])["consent_at"]
        assert c.delete(f"/api/coach/students/{s['id']}").json()["ok"]
        actions = [e["action"] for e in m.store.audit_entries(10, m.store.user_by_email("consent-coach@test.com")["id"])]
        assert actions[:2] == ["student_removed", "student_added"]


# ---- audit log --------------------------------------------------------------------------------
def test_plan_requests_and_deletions_are_audited(m):
    with client(m, "audited@test.com") as c:
        uid = m.store.user_by_email("audited@test.com")["id"]
        assert c.post("/api/upgrade-request", json={"plan": "pro"}).status_code == 200
        assert c.request("DELETE", "/api/me", json={"password": "password123"}).json()["ok"]
    rows = m.store.audit_entries(50, uid)
    assert [r["action"] for r in rows][:2] == ["account_deleted", "plan_requested"]
    assert rows[0]["actor"] == f"user:{uid}"  # the deleted account's email isn't kept


# ---- data export ---------------------------------------------------------------------------------
@needs_engine
def test_download_my_data(m):
    m.export_limiter._hits.clear()
    with client(m, "export@test.com") as c:
        demo_account(c)
        r = c.get("/api/me/export")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert {"README.txt", "account.json", "activity.json", "training.json"} <= set(names)
        assert set(json.loads(z.read("training.json"))) == {"puzzle_skill", "twin_of_you", "think_moments"}
        assert any(n.startswith("games/") for n in names) and any(n.startswith("puzzles/") for n in names)
        account = json.loads(z.read("account.json"))
        assert account["user"]["email"] == "export@test.com"
        assert "pw_hash" not in account["user"] and "pw_hash" not in z.read("activity.json").decode()
        games = json.loads(z.read(next(n for n in names if n.startswith("games/"))))
        assert games and "game_id" in games[0]
        assert json.loads(z.read("activity.json"))["security_log"][-1]["action"] == "data_exported"
        for _ in range(5):
            c.get("/api/me/export")
        assert c.get("/api/me/export").status_code == 429


# ---- legal pages ----------------------------------------------------------------------------------
@pytest.mark.parametrize("page,words", [("privacy", "Grievance"), ("terms", "18 or over"), ("refunds", "7 days")])
def test_legal_pages(m, page, words):
    r = TestClient(m.app).get(f"/{page}")
    assert r.status_code == 200 and words in r.text and "Draft" in r.text


# ---- admin CLI --------------------------------------------------------------------------------------
def test_admin_cli_switch_and_audit(m):
    import os

    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "S3_", "AWS_"))}, "DB_PATH": m.settings.db_path}
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "admin.py"), *a], capture_output=True, text=True,  # noqa: E731
                                    env=env, cwd=ROOT)
    out = run("switch", "imports", "off")
    assert out.returncode == 0 and "OFF" in out.stdout, out.stderr
    switches.clear_cache()
    assert not switches.is_on(m.store, "imports")
    assert "OFF  imports" in run("switch").stdout
    assert run("switch", "bogus", "off").returncode == 1
    assert "switch_off" in run("audit", "5").stdout
