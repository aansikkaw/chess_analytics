"""Real email sending: Codespaces link detection, SMTP/Brevo/Resend, plain-English errors,
the status page judging email by what actually happened, and the owner's test button/CLI."""

import dataclasses
import importlib.util
import json
import smtplib
import socket
import ssl
from pathlib import Path

import httpx
import pytest

from app.config import load_settings
from app.mailer import MailError, Mailer, explain
from app.status import StatusChecker
from app.store import Store
from tests.test_api import client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


@pytest.fixture
def base(tmp_path):
    return dataclasses.replace(load_settings(), db_path=str(tmp_path / "mail.db"), public_url="https://plateau.example")


def fake_smtp(monkeypatch, log: list, fail: Exception | None = None):
    class FakeSMTP:
        def __init__(self, host, port, timeout, context=None):
            log.append(("connect", host, port, "ssl" if context else "plain"))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            log.append(("starttls",))

        def login(self, user, password):
            log.append(("login", user, password))
            if fail:
                raise fail

        def send_message(self, msg):
            log.append(("send", msg["From"], msg["To"], msg["Subject"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)


# ---- settings -----------------------------------------------------------------------------------------
def test_public_url_is_detected_in_codespaces(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("CODESPACE_NAME", "fluffy-space-umbrella-x7")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    assert load_settings().public_url == "https://fluffy-space-umbrella-x7-8000.app.github.dev"
    monkeypatch.setenv("PUBLIC_URL", "https://plateaubreaker.com/")
    assert load_settings().public_url == "https://plateaubreaker.com"  # an explicit setting wins
    monkeypatch.delenv("PUBLIC_URL")
    monkeypatch.delenv("CODESPACE_NAME")
    assert load_settings().public_url == "http://localhost:8000"


def test_smtp_security_follows_the_port_and_email_from_alias(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    assert load_settings().smtp_security == "ssl"
    monkeypatch.setenv("SMTP_PORT", "587")
    assert load_settings().smtp_security == "starttls"
    monkeypatch.setenv("SMTP_SECURITY", "none")
    assert load_settings().smtp_security == "none"
    monkeypatch.setenv("EMAIL_FROM", "hello@plateau.example")
    monkeypatch.setenv("SMTP_FROM", "old@plateau.example")
    assert load_settings().smtp_from == "hello@plateau.example"


def test_transport_priority(base):
    assert Mailer(base).transport == "outbox" and not Mailer(base).configured
    assert Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com")).transport == "smtp"
    assert Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com", resend_api_key="re_x")).transport == "resend"
    assert Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com", resend_api_key="re_x", brevo_api_key="xkeysib")).transport == "brevo"


# ---- SMTP (Gmail) ---------------------------------------------------------------------------------------
def test_gmail_app_password_spaces_and_sender(base, monkeypatch):
    log = []
    fake_smtp(monkeypatch, log)
    s = dataclasses.replace(base, smtp_host="smtp.gmail.com", smtp_port=587, smtp_security="starttls",
                            smtp_user="aanya.test@gmail.com", smtp_password="abcd efgh ijkl mnop")
    mailer = Mailer(s)
    assert mailer.sender == "aanya.test@gmail.com"  # no EMAIL_FROM needed for Gmail
    assert mailer.send("player@example.com", "Confirm your email", "link")
    assert log[0] == ("connect", "smtp.gmail.com", 587, "plain") and log[1] == ("starttls",)
    assert log[2] == ("login", "aanya.test@gmail.com", "abcdefghijklmnop")  # spaces from Google's display removed
    assert "aanya.test@gmail.com" in log[3][1] and log[3][2] == "player@example.com"
    assert mailer.describe()["via"].startswith("Gmail: smtp.gmail.com:587") and mailer.describe()["warnings"] == []


def test_port_465_uses_ssl(base, monkeypatch):
    log = []
    fake_smtp(monkeypatch, log)
    mailer = Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com", smtp_port=465, smtp_security="ssl",
                                        smtp_user="me@gmail.com", smtp_password="x"))
    assert mailer.send("a@example.com", "Hi", "text")
    assert log[0] == ("connect", "smtp.gmail.com", 465, "ssl") and ("starttls",) not in log


def test_failed_send_is_recorded_and_explained(base, monkeypatch):
    log = []
    fake_smtp(monkeypatch, log, fail=smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted."))
    store = Store(base.db_path)
    s = dataclasses.replace(base, smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_password="wrong")
    mailer = Mailer(s, store)
    assert mailer.send("a@example.com", "Reset your password", "link") is False
    last = store.kv_get("last_email")
    assert last["ok"] is False and "App Password" in last["error"] and "535" in last["error"]
    assert "wrong" not in last["error"]  # never the password
    with pytest.raises(MailError, match="App Password"):
        mailer.test("me@gmail.com")
    # The status page (any process) sees it, without the provider's details
    checker = StatusChecker(s, store, None, mailer, None, {}, lambda: [])
    row = checker.email_check()
    assert row["ok"] is False and row["configured"] is True and "App Password" not in row["detail"]
    # Fixed: the next email goes out and the row turns green again
    fake_smtp(monkeypatch, log)
    mailer.test("me@gmail.com")
    row = checker.email_check()
    assert row["ok"] is True and row["last_ok_at"] and store.kv_get("last_email")["ok"] is True


def test_a_bad_recipient_does_not_mark_email_broken(base, monkeypatch):
    log = []
    fake_smtp(monkeypatch, log, fail=smtplib.SMTPRecipientsRefused({"typo@exmple": (550, b"no such user")}))
    store = Store(base.db_path)
    mailer = Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_password="x"), store)
    assert mailer.send("typo@exmple", "Confirm your email", "link") is False
    assert store.kv_get("last_email") is None


@pytest.mark.parametrize("exc, expect", [
    (TimeoutError("timed out"), "SMTP_PORT=465"),
    (ConnectionRefusedError(111, "refused"), "SMTP_PORT"),
    (socket.gaierror(-2, "Name or service not known"), "SMTP_HOST"),
    (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), "SMTP_SECURITY"),
    (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "587 needs starttls"),
    (smtplib.SMTPSenderRefused(553, b"not owned by user", "x@other.com"), "EMAIL_FROM"),
    (smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."), "SMTP_SECURITY=ssl"),
    (smtplib.SMTPAuthenticationError(535, b"Authentication failed"), "SMTP login"),
])
def test_common_smtp_failures_are_explained(base, exc, expect):
    s = dataclasses.replace(base, smtp_host="smtp.example.com", smtp_port=587, smtp_user="u", smtp_password="p")
    err = explain(exc, s, "hello@plateau.example")
    assert expect in str(err) and err.service is True


def test_a_hang_up_is_explained_by_how_far_we_got(base, monkeypatch):
    # Some servers drop the connection after a bad password instead of answering 535.
    log = []
    fake_smtp(monkeypatch, log, fail=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    gmail = Mailer(dataclasses.replace(base, smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_password="x"))
    with pytest.raises(MailError, match="hung up during sign-in.*App Password"):
        gmail.test("me@gmail.com")
    s = dataclasses.replace(base, smtp_host="smtp.example.com", smtp_user="u", smtp_password="p")
    assert "limit" in str(explain(smtplib.SMTPServerDisconnected("x"), s, "a@b.c", "send"))
    assert "587 needs starttls" in str(explain(smtplib.SMTPServerDisconnected("x"), s, "a@b.c", "connect"))


def test_warnings_catch_settings_that_will_fail(base):
    def w(**kw):
        return " ".join(Mailer(dataclasses.replace(base, **kw)).warnings())
    assert "SMTP_SECURITY=ssl" in w(smtp_host="smtp.example.com", smtp_port=465, smtp_security="starttls", smtp_user="u")
    assert "starttls" in w(smtp_host="smtp.example.com", smtp_port=587, smtp_security="ssl", smtp_user="u")
    assert "PUBLIC_URL" in w(smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", public_url="http://localhost:8000")
    assert "silently drop" in w(brevo_api_key="k", smtp_from="me@gmail.com")
    assert "EMAIL_FROM" in w(brevo_api_key="k")
    assert "alias" in w(smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_from="hello@plateau.example")
    assert w() == ""  # not set up: nothing to warn about


# ---- HTTPS APIs ---------------------------------------------------------------------------------------
def api_mailer(base, handler, **kw):
    return Mailer(dataclasses.replace(base, smtp_from="hello@plateau.example", **kw),
                  http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_brevo_api(base):
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(201, json={"messageId": "<1@relay>"})
    mailer = api_mailer(base, ok, brevo_api_key="xkeysib-123")
    assert mailer.send("player@example.com", "[Support] Hi", "plain only", reply_to="fan@example.com",
                       headers={"List-Unsubscribe": "<https://plateau.example/u>"})
    r = seen[0]
    body = json.loads(r.content)
    assert str(r.url) == "https://api.brevo.com/v3/smtp/email" and r.headers["api-key"] == "xkeysib-123"
    assert body["sender"] == {"name": "Plateau Breaker", "email": "hello@plateau.example"} and body["to"] == [{"email": "player@example.com"}]
    assert body["replyTo"] == {"email": "fan@example.com"} and body["headers"]["List-Unsubscribe"]
    assert "plain only" in body["htmlContent"] and body["textContent"] == "plain only"  # HTML is always included


@pytest.mark.parametrize("status, payload, expect, service", [
    (401, {"code": "unauthorized", "message": "We have detected you are using an unrecognised IP address 4.3.2.1."}, "Authorised IPs", True),
    (401, {"code": "unauthorized", "message": "Key not found"}, "BREVO_API_KEY", True),
    (400, {"code": "invalid_parameter", "message": "Sender is invalid / inactive"}, "EMAIL_FROM", True),
    (403, {"code": "permission_denied", "message": "Unable to send email. Your SMTP account is not yet activated."}, "activate", True),
    (400, {"code": "invalid_parameter", "message": "email is not valid in to"}, "recipient", False),
    (429, {"message": "Too many requests"}, "limit", True),
])
def test_brevo_errors_are_explained(base, status, payload, expect, service):
    mailer = api_mailer(base, lambda request: httpx.Response(status, json=payload), brevo_api_key="k")
    with pytest.raises(MailError) as e:
        mailer.test("me@example.com")
    assert expect in str(e.value) and e.value.service is service


def test_resend_api(base):
    seen = []

    def handler(request):
        seen.append(request)
        if json.loads(request.content)["to"] == ["stranger@example.com"]:
            return httpx.Response(403, json={"statusCode": 403, "name": "validation_error",
                                             "message": "You can only send testing emails to your own email address."})
        return httpx.Response(200, json={"id": "abc"})
    mailer = api_mailer(base, handler, resend_api_key="re_123")
    assert mailer.send("me@example.com", "Hi", "text", "<p>html</p>", reply_to="fan@example.com")
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer re_123" and body["from"] == "Plateau Breaker <hello@plateau.example>"
    assert body["html"] == "<p>html</p>" and body["reply_to"] == "fan@example.com"
    with pytest.raises(MailError, match="domain is verified"):
        mailer.test("stranger@example.com")


def test_unreachable_api_is_explained(base):
    def boom(request):
        raise httpx.ConnectError("connection failed")
    with pytest.raises(MailError, match="Couldn't reach the email service"):
        api_mailer(base, boom, brevo_api_key="k").test("me@example.com")


# ---- owner tools: account-panel button and CLI ------------------------------------------------------------
def test_owner_email_endpoints(m, monkeypatch):
    m.test_email_limiter._hits.clear()
    player = client(m, "not-the-owner@test.com")
    assert player.get("/api/admin/email").status_code == 403
    assert player.post("/api/admin/email/test").status_code == 403
    owner = client(m, "admin@test.com")
    info = owner.get("/api/admin/email").json()
    assert info["configured"] is False and info["transport"] == "outbox"
    r = owner.post("/api/admin/email/test").json()
    assert r["ok"] and "outbox" in r["message"]
    # Configured but failing: the owner sees why
    broken = Mailer(dataclasses.replace(m.settings, smtp_host="smtp.gmail.com", smtp_user="admin@gmail.com", smtp_password="x"))
    monkeypatch.setattr(broken, "deliver", lambda mail: (_ for _ in ()).throw(MailError("Gmail didn't accept the login.")))
    monkeypatch.setattr(m, "mailer", broken)
    r = owner.post("/api/admin/email/test").json()
    assert r["ok"] is False and "Gmail didn't accept" in r["error"] and r["transport"] == "smtp"
    for _ in range(10):
        owner.post("/api/admin/email/test")
    assert owner.post("/api/admin/email/test").status_code == 429
    m.test_email_limiter._hits.clear()


def _admin_script():
    spec = importlib.util.spec_from_file_location("admin_script_email", ROOT / "scripts" / "admin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_admin_cli_test_email(monkeypatch, capsys, tmp_path):
    admin = _admin_script()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    assert admin.main(["test-email", "me@example.com"]) == 1  # not set up: saved to outbox, says how to fix
    out = capsys.readouterr().out
    assert "NOT set up" in out and "outbox" in out and list((tmp_path / "outbox").glob("*.eml"))
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("PUBLIC_URL", "https://plateau.example")
    log = []
    fake_smtp(monkeypatch, log, fail=smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted."))
    assert admin.main(["test-email", "me@example.com"]) == 1
    out = capsys.readouterr().out
    assert "Not sent" in out and "App Password" in out and "abcdefghijklmnop" not in out
    fake_smtp(monkeypatch, log)
    assert admin.main(["test-email", "me@example.com"]) == 0
    assert "✓ Sent" in capsys.readouterr().out
    assert admin.main(["email"]) == 0
    out = capsys.readouterr().out
    assert "Gmail: smtp.gmail.com:587" in out and "Last email:  sent" in out
    assert admin.main(["test-email"]) == 1
