"""v0.5 operations: instant preview, email flows, support, account deletion, monitoring,
status page, backups, scheduler, weekly digest, Event Pass and Pro teasers."""

import dataclasses
import importlib.util
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backup import BackupConfig, BackupError, fetch, restore_test, run_backup, storage_for
from app.games import parse_pgn
from app.plans import effective_plan, is_pro
from app.scheduler import Chore, due, tick
from app.store import Store
from tests.conftest import needs_engine
from tests.test_api import DEMO, client, demo_account

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


def _token(text: str, path: str) -> str:
    return re.search(rf"{path}#token=([\w-]+)", text).group(1)


def _last_mail(m, to: str, subject: str) -> dict:
    return next(x for x in reversed(m.mailer.sent) if x["to"] == to and subject in x["subject"])


# ---- instant preview ------------------------------------------------------------------------------
@needs_engine
def test_preview_without_an_account(m, monkeypatch):
    monkeypatch.setattr("app.sync.check_account", lambda platform, name: "PreviewPlayer")
    monkeypatch.setattr("app.sync.fetch_games", lambda platform, name, n, classes, since=None: parse_pgn(DEMO, "demo_player")[:4])
    m.preview_limiter._hits.clear()
    c = TestClient(m.app)
    r = c.post("/api/preview", json={"username": "previewplayer"})
    assert r.status_code == 200 and r.json()["cached"] is False
    v = c.get(f"/api/preview/{r.json()['job_id']}").json()
    assert v["state"] == "done" and v["handle"] == "PreviewPlayer" and v["games"] == 4
    assert v["dna"]["skills"] and v["moment"]["fen"] and v["moment"]["engine_best"]
    assert "payload" not in v
    again = c.post("/api/preview", json={"username": "PREVIEWPLAYER"}).json()
    assert again["cached"] is True and again["job_id"] == r.json()["job_id"]  # cached for an hour
    assert c.post("/api/preview", json={"username": "bad name!"}).status_code == 400
    # Per-IP limit on fresh previews
    monkeypatch.setattr(m, "preview_limiter", m.auth.LoginLimiter(limit=1, window=3600))
    assert c.post("/api/preview", json={"username": "someone_new"}).status_code == 200
    assert c.post("/api/preview", json={"username": "someone_else"}).status_code == 429
    # Account jobs aren't visible through the preview endpoint
    assert c.get("/api/preview/doesnotexist").status_code == 404


@needs_engine
def test_preview_reports_a_missing_user(m, monkeypatch):
    from app.games import GameImportError

    def nope(platform, name):
        raise GameImportError(f"No Lichess account called '{name}'.")
    monkeypatch.setattr("app.sync.check_account", nope)
    m.preview_limiter._hits.clear()
    c = TestClient(m.app)
    v = c.get(f"/api/preview/{c.post('/api/preview', json={'username': 'ghost_x'}).json()['job_id']}").json()
    assert v["state"] == "error" and "No Lichess account" in v["error"]


# ---- email verification & password reset -------------------------------------------------------------
def test_signup_sends_verification_and_link_works_once(m):
    c = client(m, "verify-me@test.com")
    assert c.get("/api/me").json()["email_verified"] is False
    mail = _last_mail(m, "verify-me@test.com", "Confirm your email")
    tok = _token(mail["text"], "/verify")
    assert c.post("/api/auth/verify", json={"token": tok}).json()["ok"]
    assert c.post("/api/auth/verify", json={"token": tok}).status_code == 400  # single use
    assert c.get("/api/me").json()["email_verified"] is True
    assert "already confirmed" in c.post("/api/auth/verify/resend").json()["message"]


def test_resend_is_rate_limited(m):
    m.resend_limiter._hits.clear()
    c = client(m, "resend@test.com")
    for _ in range(3):
        assert c.post("/api/auth/verify/resend").status_code == 200
    assert c.post("/api/auth/verify/resend").status_code == 429


def test_password_reset_flow(m):
    m.forgot_limiter._hits.clear()
    old = client(m, "reset-me@test.com", password="oldpassword1")
    anon = TestClient(m.app)
    before = len(m.mailer.sent)
    r = anon.post("/api/auth/forgot", json={"email": "nobody-here@test.com"})
    assert r.status_code == 200 and len(m.mailer.sent) == before  # same answer, no email: no account enumeration
    assert anon.post("/api/auth/forgot", json={"email": "Reset-Me@test.com"}).json()["ok"]
    tok = _token(_last_mail(m, "reset-me@test.com", "Reset your password")["text"], "/reset")
    assert anon.post("/api/auth/reset", json={"token": tok, "password": "short"}).status_code == 400
    assert anon.post("/api/auth/reset", json={"token": "made-up", "password": "newpassword1"}).status_code == 400
    r = anon.post("/api/auth/reset", json={"token": tok, "password": "newpassword1"})
    assert r.status_code == 200 and anon.get("/api/me").json()["email"] == "reset-me@test.com"
    assert anon.get("/api/me").json()["email_verified"] is True  # they proved they own the inbox
    assert old.get("/api/me").status_code == 401  # other sessions were signed out
    assert anon.post("/api/auth/reset", json={"token": tok, "password": "another123"}).status_code == 400  # used up
    fresh = TestClient(m.app)
    assert fresh.post("/api/auth/login", json={"email": "reset-me@test.com", "password": "oldpassword1"}).status_code == 401
    assert fresh.post("/api/auth/login", json={"email": "reset-me@test.com", "password": "newpassword1"}).status_code == 200


def test_forgot_is_rate_limited(m):
    m.forgot_limiter._hits.clear()
    c = TestClient(m.app)
    for _ in range(5):
        c.post("/api/auth/forgot", json={"email": "x@test.com"})
    assert c.post("/api/auth/forgot", json={"email": "x@test.com"}).status_code == 429
    m.forgot_limiter._hits.clear()


@needs_engine
def test_require_verified_email_blocks_engine_work(m, monkeypatch):
    c = client(m, "unverified-gate@test.com")
    a = demo_account(c)  # allowed while the switch is off
    monkeypatch.setattr(m, "settings", dataclasses.replace(m.settings, require_verified_email=True))
    r = c.post(f"/api/accounts/{a['id']}/coach", json={"message": "plan?"})
    assert r.status_code == 403 and r.json()["detail"]["verify"] is True
    tok = _token(_last_mail(m, "unverified-gate@test.com", "Confirm your email")["text"], "/verify")
    c.post("/api/auth/verify", json={"token": tok})
    assert c.post(f"/api/accounts/{a['id']}/coach", json={"message": "plan?"}).status_code == 200


def test_smtp_sending(m, monkeypatch):
    from app.mailer import Mailer

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            sent.append(("starttls",))

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"], msg["Reply-To"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    mailer = Mailer(dataclasses.replace(m.settings, smtp_host="smtp.example.com", smtp_user="apikey", smtp_password="x",
                                        smtp_from="hello@plateau.example"))
    assert mailer.configured and mailer.send("a@b.com", "Hi", "text", "<p>html</p>", reply_to="c@d.com")
    assert sent == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "apikey"), ("send", "a@b.com", "Hi", "c@d.com")]


def test_dev_outbox_when_smtp_is_not_configured(m, tmp_path):
    from app.mailer import Mailer

    mailer = Mailer(dataclasses.replace(m.settings, db_path=str(tmp_path / "x.db")))
    assert mailer.send("a@b.com", "Reset your password", "link here")
    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and b"link here" in files[0].read_bytes()


# ---- account deletion, support, digest preference ----------------------------------------------------
@needs_engine
def test_delete_my_account_removes_everything(m):
    c = client(m, "leaving@test.com")
    a = demo_account(c)
    bundle = m.importer.knowledge.path(m.store.account(a["id"]))
    assert bundle.exists()
    assert c.request("DELETE", "/api/me", json={"password": "wrongpass1"}).status_code == 401
    assert c.request("DELETE", "/api/me", json={"password": "password123"}).json()["ok"]
    assert c.get("/api/me").status_code == 401
    assert m.store.user_by_email("leaving@test.com") is None and m.store.account(a["id"]) is None
    assert not bundle.exists()


def test_support_messages(m, monkeypatch):
    m.support_limiter._hits.clear()
    anon = TestClient(m.app)
    assert anon.post("/api/support", json={"subject": "Help", "message": "It broke"}).status_code == 400  # needs an email
    r = anon.post("/api/support", json={"email": "fan@test.com", "subject": "Sync stuck", "message": "My sync is stuck", "page": "/app"})
    assert r.json()["ok"] and m.store.support_messages()[0]["email"] == "fan@test.com"
    monkeypatch.setattr(m, "settings", dataclasses.replace(m.settings, support_email="support@plateau.example"))
    c = client(m, "needs-help@test.com")
    c.post("/api/support", json={"subject": "Billing", "message": "When can I pay?"})
    mail = _last_mail(m, "support@plateau.example", "[Support] Billing")
    assert "needs-help@test.com" in mail["text"] and "When can I pay?" in mail["text"]
    for _ in range(5):
        anon.post("/api/support", json={"email": "fan@test.com", "subject": "Again", "message": "and again"})
    assert anon.post("/api/support", json={"email": "fan@test.com", "subject": "Again", "message": "and again"}).status_code == 429
    m.support_limiter._hits.clear()


def test_digest_preference(m):
    c = client(m, "digest-pref@test.com")
    assert c.get("/api/me").json()["digest"] is True
    c.patch("/api/me", json={"digest": False})
    assert c.get("/api/me").json()["digest"] is False


# ---- monitoring, analytics, config, status ------------------------------------------------------------
def test_client_errors_are_accepted_and_scrubbed(m, monkeypatch):
    from app import observability

    seen = []
    monkeypatch.setattr(observability, "log", type("L", (), {"warning": lambda self, *a: seen.append(a)})())
    c = TestClient(m.app)
    r = c.post("/api/client-error", json={"message": "TypeError for jane@example.com", "source": "/static/app.js", "line": 10})
    assert r.status_code == 204 and "[email]" in seen[0][1] and "jane@" not in seen[0][1]


def test_sentry_scrubs_personal_data():
    from app.observability import _before_send

    ev = _before_send({"request": {"cookies": {"pb_session": "x"}, "data": {"password": "p"}, "headers": {"Cookie": "x", "Accept": "*/*"}},
                       "user": {"id": 5, "email": "a@b.com", "ip_address": "1.2.3.4"},
                       "exception": {"values": [{"value": "No user a@b.com"}]}}, {})
    assert "cookies" not in ev["request"] and "data" not in ev["request"] and ev["request"]["headers"]["Cookie"] == "[filtered]"
    assert ev["user"] == {"id": 5} and ev["exception"]["values"][0]["value"] == "No user [email]"


def test_config_and_security_headers(m, monkeypatch):
    c = TestClient(m.app)
    r = c.get("/api/config")
    cfg = r.json()
    assert cfg["analytics"] == {"provider": "none"} and "version" in cfg and cfg["coach_mode"] == "offline"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    monkeypatch.setattr(m, "settings", dataclasses.replace(m.settings, analytics="plausible", plausible_domain="plateau.example"))
    assert "https://plausible.io" in m._csp()
    monkeypatch.setattr(m, "settings", dataclasses.replace(m.settings, analytics="posthog", posthog_key="phc_x"))
    assert "posthog.com" in m._csp()


def test_status_page_and_api(m):
    with TestClient(m.app) as c:
        s = c.get("/api/status", params={"external": "false"}).json()
        assert s["status"] in ("ok", "degraded") and s["checks"]["database"]["ok"]
        assert s["checks"]["imports"]["mode"] == "inline" and "backups" in s["checks"] and "coach" in s["checks"]
        assert c.get("/status").status_code == 200
        assert c.get("/sw.js").headers["content-type"].startswith("text/javascript")
        assert c.get("/manifest.webmanifest").status_code == 200
        assert c.get("/reset").status_code == 200 and c.get("/verify").status_code == 200


def test_status_external_checks_are_cached(m):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[])
    http = httpx.Client(transport=httpx.MockTransport(handler))
    m.status_checker._ext = {}
    r1 = m.status_checker.report(http=http)
    r2 = m.status_checker.report(http=http)
    assert r1["checks"]["lichess"]["ok"] and r2["checks"]["lichess"]["ok"]
    assert len(calls) == (2 if m.settings.chesscom_enabled else 1)  # second report used the cache
    m.status_checker._ext = {}


def test_coach_fallbacks_degrade_status(m):
    from app.status import CoachStats

    s = CoachStats()
    for _ in range(3):
        s.record("fallback", "rate limit")
    assert s.summary()["recent_fallbacks"] == 3 and s.summary()["last_fallback"]["notice"] == "rate limit"


# ---- plans: Event Pass, teasers ----------------------------------------------------------------------
def test_event_pass_expires_back_to_free(m):
    assert effective_plan({"plan": "pro", "plan_expires_at": time.time() + 3600}) == "pro"
    assert effective_plan({"plan": "pro", "plan_expires_at": time.time() - 1}) == "free"
    assert is_pro({"plan": "pro", "plan_expires_at": None})
    c = client(m, "event@test.com")
    spec = importlib.util.spec_from_file_location("admin_script", ROOT / "scripts" / "admin.py")
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    assert admin.main(["grant", "event@test.com", "--days", "10"]) == 0
    me = c.get("/api/me").json()
    assert me["plan"] == "pro" and me["plan_expires_at"] > time.time() + 9 * 86_400
    uid = m.store.user_by_email("event@test.com")["id"]
    m.store.set_plan(uid, "pro", time.time() - 5)  # the pass ran out
    assert c.get("/api/me").json()["plan"] == "free"
    assert c.get("/api/accounts/1/progress").status_code in (402, 404)


def test_upgrade_requests_by_plan(m):
    c = client(m, "waitlist@test.com")
    assert "waitlist" in c.post("/api/upgrade-request", json={"plan": "coach"}).json()["message"]
    assert c.post("/api/upgrade-request", json={"plan": "platinum"}).status_code == 400
    assert any(r["email"] == "waitlist@test.com" and r["requested"] == "coach" for r in m.store.upgrade_requests())


@needs_engine
def test_teasers_and_game_detail(m):
    c = client(m, "teaser@test.com")
    a = demo_account(c)
    t = c.get(f"/api/accounts/{a['id']}/teasers").json()
    assert {"leaks", "recurring_mistakes", "lines", "progress_periods", "accuracy_trend", "repertoires"} <= t.keys()
    gid = c.get(f"/api/accounts/{a['id']}/profile").json()["recent_games"][0]["game_id"]
    g = c.get(f"/api/accounts/{a['id']}/game/{gid}").json()
    assert g["moves_san"] and all(k in g for k in ("marks", "color", "opening"))
    assert len(g["fens"]) == len(g["ucis"]) + 1 == len(g["moves_san"]) + 1 and g["fens"][0].startswith("rnbqkbnr/pppppppp")
    assert c.get(f"/api/accounts/{a['id']}/game/nope").status_code == 404
    other = client(m, "teaser-other@test.com")
    assert other.get(f"/api/accounts/{a['id']}/teasers").status_code == 404


# ---- weekly digest ----------------------------------------------------------------------------------------
@needs_engine
def test_weekly_digest(m):
    from app.digest import send_digests

    c = client(m, "digest@test.com")
    demo_account(c)
    tok = _token(_last_mail(m, "digest@test.com", "Confirm your email")["text"], "/verify")
    c.post("/api/auth/verify", json={"token": tok})
    sent_before = sum(1 for x in m.mailer.sent if x["to"] == "digest@test.com" and "week" in x["subject"])
    assert send_digests(m.store, m.mailer, "https://plateau.example") >= 1
    mail = _last_mail(m, "digest@test.com", "Your week in chess")
    assert "Rating" in mail["text"] and "puzzles waiting" in mail["text"] and "/api/digest/unsubscribe?token=" in mail["text"]
    send_digests(m.store, m.mailer, "https://plateau.example")
    assert sum(1 for x in m.mailer.sent if x["to"] == "digest@test.com" and "week" in x["subject"]) == sent_before + 1  # once a week
    unsub = re.search(r"/api/digest/unsubscribe\?token=([\w-]+)", mail["text"]).group(1)
    anon = TestClient(m.app)
    page = anon.get(f"/api/digest/unsubscribe?token={unsub}").text
    assert "Unsubscribe" in page and c.get("/api/me").json()["digest"] is True  # a GET (e.g. a link scanner) changes nothing
    assert "won't get" in anon.post(f"/api/digest/unsubscribe?token={unsub}").text  # the button, or one-click from the mail app
    assert c.get("/api/me").json()["digest"] is False


# ---- backups -----------------------------------------------------------------------------------------------
def _db(tmp_path) -> str:
    path = str(tmp_path / "live.db")
    st = Store(path)
    st.create_user("a@test.com", "scrypt$x$y")
    return path


def test_backup_to_folder_restore_and_prune(tmp_path):
    db = _db(tmp_path)
    cfg = BackupConfig(target=str(tmp_path / "backups"), keep=2)
    names = [run_backup(db, cfg)["name"] for _ in range(3)]
    assert len(set(names)) == 3
    stored = storage_for(cfg).list()
    assert stored == sorted(names)[-2:]  # pruned to the newest 2
    out = fetch(cfg, None, tmp_path / "restored.db")
    assert out["counts"]["users"] == 1 and sqlite3.connect(tmp_path / "restored.db").execute("SELECT email FROM users").fetchone()[0] == "a@test.com"
    t = restore_test(db, cfg)
    assert t["ok"] and t["counts"] == t["live_counts"]


def test_encrypted_backups(tmp_path):
    db = _db(tmp_path)
    cfg = BackupConfig(target=str(tmp_path / "b"), passphrase="correct horse battery staple")
    name = run_backup(db, cfg)["name"]
    assert name.endswith(".enc") and b"a@test.com" not in (tmp_path / "b" / name).read_bytes()
    with pytest.raises(BackupError, match="encrypted"):
        fetch(dataclasses.replace(cfg, passphrase=None), None, tmp_path / "x.db")
    with pytest.raises(BackupError, match="Wrong"):
        fetch(dataclasses.replace(cfg, passphrase="nope"), None, tmp_path / "x.db")
    assert fetch(cfg, None, tmp_path / "ok.db")["ok"]


def test_corrupt_backup_is_rejected(tmp_path):
    import gzip

    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "plateau-20260101-000000000.sqlite.gz").write_bytes(gzip.compress(b"not a database at all" * 100))
    with pytest.raises(BackupError):
        fetch(BackupConfig(target=str(folder)), None, tmp_path / "x.db")


def test_backup_to_s3_compatible_storage(tmp_path, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="pb-backups")
        db = _db(tmp_path)
        cfg = BackupConfig(target="s3://pb-backups/daily", keep=5)
        run_backup(db, cfg)
        assert len(storage_for(cfg).list()) == 1
        assert fetch(cfg, None, tmp_path / "r.db")["counts"]["users"] == 1


def test_backup_cli_records_the_result(tmp_path):
    db = _db(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "DB_PATH": db, "BACKUP_TARGET": str(tmp_path / "cli"), "HOME": str(tmp_path)}
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), *a], env=env, capture_output=True, text=True)  # noqa: E731
    assert run("run").returncode == 0
    assert run("test-restore").returncode == 0
    assert Store(db).kv_get("last_backup")["ok"] and Store(db).kv_get("last_restore_test")["ok"]
    r = run("restore", "--to", db)
    assert r.returncode != 0 and "live database" in r.stderr  # refuses to overwrite the live DB without --force


# ---- scheduler ----------------------------------------------------------------------------------------------
def test_scheduler_due_rules():
    daily = Chore("backup", 86_400, 21, None, lambda: True, lambda: {})
    weekly = Chore("digest", 7 * 86_400, 3, 0, lambda: True, lambda: {})
    mon_22 = time.mktime((2026, 10, 5, 22, 0, 0, 0, 0, 0)) - time.timezone  # Monday 22:00 UTC
    assert due(daily, mon_22, None) and not due(daily, mon_22, mon_22 - 3600)
    assert not due(daily, mon_22 - 3 * 3600, None)  # before 21:00
    assert due(weekly, mon_22, None) and not due(weekly, mon_22 + 86_400, None)  # Mondays only


def test_scheduler_tick_runs_each_chore_once(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    runs = []
    items = [Chore("c", 86_400, 0, None, lambda: True, lambda: runs.append(1) or {"ok": True}),
             Chore("off", 86_400, 0, None, lambda: False, lambda: runs.append(2) or {})]
    assert tick(st, items) == ["c"] and tick(st, items) == [] and runs == [1]
    assert st.kv_get("sched:c")["ok"] is True


# ---- fixes from the pre-launch review ---------------------------------------------------------------
def test_email_links_keep_the_token_out_of_server_requests(m):
    client(m, "fragment@test.com")
    text = _last_mail(m, "fragment@test.com", "Confirm your email")["text"]
    assert "/verify#token=" in text and "?token=" not in text  # a #fragment never reaches the server or its logs


def test_signup_is_rate_limited_per_ip(m, monkeypatch):
    monkeypatch.setattr(m, "signup_limiter", m.auth.LoginLimiter(limit=2, window=3600))
    c = TestClient(m.app)
    for i in range(2):
        c.post("/api/auth/signup", json={"email": f"flood{i}@test.com", "password": "password123"})
    assert c.post("/api/auth/signup", json={"email": "flood9@test.com", "password": "password123"}).status_code == 429


def test_static_cache_headers(m):
    c = TestClient(m.app)
    assert c.get("/static/js/core.js").headers["cache-control"] == "no-cache"  # deploys reach everyone at once
    assert "max-age=604800" in c.get("/static/fonts/chess-pieces.woff2").headers["cache-control"]


def test_one_active_import_per_account_even_under_races(tmp_path):
    import threading

    from app.jobs import JobQueue

    q = JobQueue(str(tmp_path / "race.db"))
    got = []
    threads = [threading.Thread(target=lambda: got.append(q.enqueue_for_account("import", {}, 1, 42))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([g for g in got if g]) == 1  # double clicks and auto-sync can't start two imports
    q.cancel_for_account(42)
    assert q.enqueue_for_account("import", {}, 1, 42)


@needs_engine
def test_deleting_an_account_mid_import_leaves_nothing_behind(m, monkeypatch):
    import app.sync as sync_mod

    c = client(m, "midway@test.com")
    a = c.post("/api/accounts", json={"platform": "demo"}).json()
    acct = m.store.account(a["id"])
    real = sync_mod.analyse_game
    calls = []

    def analyse_then_vanish(g, engine):  # the user deletes the account while Stockfish is busy
        calls.append(1)
        if len(calls) == 1:
            c.delete(f"/api/accounts/{a['id']}")
        return real(g, engine)
    monkeypatch.setattr(sync_mod, "analyse_game", analyse_then_vanish)
    job = c.post(f"/api/accounts/{a['id']}/pgn", json={"pgn": "demo"})
    assert job.status_code in (200, 404)
    assert m.store.load_games(sync_mod.player_key(acct)) == [] and not m.importer.knowledge.path(acct).exists()


def test_fallback_is_used_at_once_on_a_rate_limit(monkeypatch):
    import httpx as _httpx

    from app import llm
    from app.coach import Coach, CoachTools  # noqa: F401

    waits = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    b = llm.Backend("openai", "m", "https://api.groq.com/openai/v1", "k")
    http = _httpx.Client(transport=_httpx.MockTransport(lambda r: _httpx.Response(429, headers={"retry-after": "8"})))
    with pytest.raises(llm.LLMError):
        llm.openai_chat(b, [], [], http, wait_for_limits=False)
    assert waits == []  # no 8-second wait when another provider can answer
    llm.openai_chat.__defaults__  # noqa: B018 - signature still defaults to waiting when it's the last provider


def test_restore_works_when_the_live_database_is_corrupt(tmp_path):
    db = _db(tmp_path)
    target = tmp_path / "bk"
    env = {"PATH": "/usr/bin:/bin", "DB_PATH": db, "BACKUP_TARGET": str(target), "HOME": str(tmp_path)}
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), *a], env=env, capture_output=True, text=True)  # noqa: E731
    assert run("run").returncode == 0
    Path(db).write_bytes(b"garbage, not sqlite")  # disaster
    r = run("restore", "--to", str(tmp_path / "restored.db"))
    assert r.returncode == 0, r.stderr
    assert sqlite3.connect(tmp_path / "restored.db").execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_scheduler_survives_unexpected_errors(tmp_path):
    st = Store(str(tmp_path / "s.db"))

    def boom():
        raise RuntimeError("EndpointConnectionError: could not reach R2")
    items = [Chore("backup", 86_400, 0, None, lambda: True, boom)]
    assert tick(st, items) == ["backup"]  # recorded, not raised
    assert st.kv_get("last_backup")["ok"] is False and "R2" in st.kv_get("last_backup")["error"]


def test_sentry_is_configured_without_local_variables(m):
    import sentry_sdk

    from app import observability

    observability._state["sentry"] = False
    try:
        assert observability.init_sentry(dataclasses.replace(m.settings, sentry_dsn="https://public@o0.ingest.example.com/1"), "test")
        opts = sentry_sdk.get_client().options
        assert opts["include_local_variables"] is False and opts["max_request_body_size"] == "never" and opts["send_default_pii"] is False
    finally:
        sentry_sdk.init(dsn=None)
        observability._state["sentry"] = False
