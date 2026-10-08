"""Outgoing email: verification, password reset, support messages and the weekly digest.

Three ways to send, picked from the settings (first one set wins):

  BREVO_API_KEY    Brevo's HTTPS API. Uses port 443, so it works even on hosts that block mail ports.
  RESEND_API_KEY   Resend's HTTPS API, likewise.
  SMTP_HOST        Any SMTP server: Gmail with an app password, Brevo, Postmark, Amazon SES, Zoho, ...

EMAIL_FROM (or SMTP_FROM) is the address emails come from.

With none of them set, nothing is sent: each email is written to <DB folder>/outbox/ and logged,
so you can click the links while developing.

`python scripts/admin.py test-email you@example.com` sends a test message and, if it fails,
says what to change. Every send records its result, so the status page shows whether email
is actually going out, not just whether it's configured.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings

log = logging.getLogger("plateau.mail")
BRAND = "Plateau Breaker"
TIMEOUT_S = 20
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
             "me.com", "aol.com", "proton.me", "protonmail.com", "rediffmail.com", "ymail.com"}


class MailError(Exception):
    """A send failed. str() explains what went wrong and what to change.

    `service` is False when the problem is the recipient (a mistyped address), not the
    mail setup, so one bad address doesn't mark email as broken on the status page.
    """

    def __init__(self, message: str, service: bool = True):
        super().__init__(message)
        self.service = service


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    headers: dict = field(default_factory=dict)


class Mailer:
    def __init__(self, settings: Settings, store=None, http: httpx.Client | None = None):
        self.s = settings
        self.store = store  # where the last result is recorded for the status page (shared by all processes)
        self.outbox = Path(settings.db_path).resolve().parent / "outbox"
        self.sent: deque[dict] = deque(maxlen=50)  # recent messages, for tests and the dev outbox
        self.last: dict | None = None
        self._http = http
        self._lock = threading.Lock()

    # ---- how mail goes out -----------------------------------------------------------------
    @property
    def transport(self) -> str:
        if self.s.brevo_api_key:
            return "brevo"
        if self.s.resend_api_key:
            return "resend"
        if self.s.smtp_host:
            return "smtp"
        return "outbox"

    @property
    def configured(self) -> bool:
        return self.transport != "outbox"

    @property
    def sender(self) -> str:
        s = self.s
        if s.smtp_from:
            return s.smtp_from
        if self.transport == "smtp" and _is_gmail(s.smtp_host) and s.smtp_user and "@" in s.smtp_user:
            return s.smtp_user  # Gmail sends as the signed-in account anyway
        return s.support_email or "no-reply@localhost"

    def describe(self) -> dict:
        """What the owner sees about the setup. Never includes a password or key."""
        t, s = self.transport, self.s
        via = {"brevo": "Brevo API (HTTPS)", "resend": "Resend API (HTTPS)", "outbox": "Not sent: saved to the outbox/ folder"}.get(t)
        if t == "smtp":
            via = f"{'Gmail' if _is_gmail(s.smtp_host) else 'SMTP'}: {s.smtp_host}:{s.smtp_port} ({s.smtp_security.upper()})"
        return {"configured": self.configured, "transport": t, "via": via, "from": self.sender,
                "public_url": s.public_url, "warnings": self.warnings(), "last": self.last_result()}

    def warnings(self) -> list[str]:
        """Settings that will probably stop email working, in plain words."""
        t, s, out = self.transport, self.s, []
        if t == "outbox":
            return out
        sender_domain = self.sender.rsplit("@", 1)[-1].lower()
        if self.sender.endswith("@localhost"):
            out.append("Set EMAIL_FROM to the address emails should come from.")
        elif t in ("brevo", "resend") and sender_domain in FREE_MAIL:
            out.append(f"{'Brevo' if t == 'brevo' else 'Resend'} can't authenticate a {sender_domain} sender, so Gmail and Yahoo "
                       "may silently drop these emails. Send from an address at your own domain, or send through Gmail "
                       "itself (SMTP_HOST=smtp.gmail.com).")
        if t == "smtp":
            if s.smtp_port == 465 and s.smtp_security != "ssl":
                out.append("Port 465 needs SMTP_SECURITY=ssl.")
            if s.smtp_port == 587 and s.smtp_security == "ssl":
                out.append("Port 587 needs SMTP_SECURITY=starttls.")
            if s.smtp_host and not s.smtp_user:
                out.append("SMTP_USER is empty: most providers need a login.")
            if _is_gmail(s.smtp_host) and s.smtp_from and s.smtp_user and s.smtp_from.lower() != s.smtp_user.lower():
                out.append(f"Gmail sends as {s.smtp_user}; EMAIL_FROM is replaced unless it's an alias added in Gmail's settings.")
        host = urlparse(s.public_url).hostname or ""
        if host in ("localhost", "127.0.0.1", "0.0.0.0"):
            out.append("PUBLIC_URL is localhost, so links in emails only open on this computer. Set PUBLIC_URL to the address people use.")
        return out

    def last_result(self) -> dict | None:
        if self.store is not None:
            try:
                return self.store.kv_get("last_email")
            except Exception:  # noqa: BLE001 - a status read must never fail a request
                return self.last
        return self.last

    # ---- sending ---------------------------------------------------------------------------
    def send(self, to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None,
             headers: dict | None = None) -> bool:
        return self._send(Email(to, subject, text, html, reply_to, dict(headers or {}))) is None

    def test(self, to: str) -> dict:
        """Send a test message now. Raises MailError (with an explanation) if it fails."""
        info = self.describe()
        when = time.strftime("%d %b %Y, %H:%M UTC", time.gmtime())
        text = (f"This is a test from {BRAND}, sent {when}.\n\nIf you can read this, email works: verification and "
                f"password-reset emails will reach people too.\n\nSent through: {info['via']}\n"
                f"Links in emails open: {info['public_url']}\n")
        html = _html("Email works", [f"This is a test from {BRAND}, sent {escape(when)}.",
                                     "If you can read this, verification and password-reset emails will reach people too.",
                                     f"Sent through: {escape(info['via'])}<br>Links in emails open: {escape(info['public_url'])}"],
                     ("Open the status page", f"{self.s.public_url}/status"))
        err = self._send(Email(to, f"Test email from {BRAND}", text, html))
        if err:
            raise err
        return self.describe()

    def _send(self, mail: Email) -> MailError | None:
        with self._lock:
            self.sent.append({"to": mail.to, "subject": mail.subject, "text": mail.text, "at": time.time()})
        if not self.configured:
            self._write_outbox(self._mime(mail), mail.subject)
            return None
        try:
            self.deliver(mail)
        except MailError as exc:
            log.error("email to a %s address failed: %s", mail.to.rsplit("@", 1)[-1], exc)
            if exc.service:
                self._record(False, str(exc))
                _report(exc.__cause__ or exc)
            return exc
        self._record(True)
        return None

    def deliver(self, mail: Email) -> None:
        """Send one email now, or raise MailError explaining why it couldn't be sent."""
        t = self.transport
        try:
            if t == "smtp":
                self._smtp(self._mime(mail))
            elif t == "brevo":
                self._api_brevo(mail)
            elif t == "resend":
                self._api_resend(mail)
            else:
                self._write_outbox(self._mime(mail), mail.subject)
        except MailError:
            raise
        except (OSError, smtplib.SMTPException, httpx.HTTPError) as exc:  # SMTPException is an OSError too
            raise explain(exc, self.s, self.sender) from exc

    def send_async(self, *args, **kwargs) -> None:
        threading.Thread(target=self.send, args=args, kwargs=kwargs, daemon=True).start()

    def _record(self, ok: bool, error: str | None = None) -> None:
        entry = {"ok": ok, "transport": self.transport, "error": error, "at": time.time()}
        prev = self.last_result() or {}
        entry["last_ok_at"] = entry["at"] if ok else prev.get("last_ok_at")
        self.last = entry
        if self.store is not None:
            try:
                self.store.kv_set("last_email", entry)
            except Exception as exc:  # noqa: BLE001
                log.warning("couldn't record the email result: %s", exc)

    # ---- transports ------------------------------------------------------------------------
    def _mime(self, mail: Email) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((BRAND, self.sender))
        msg["To"] = mail.to
        msg["Subject"] = mail.subject
        msg["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1])
        if mail.reply_to:
            msg["Reply-To"] = mail.reply_to
        for k, v in mail.headers.items():
            msg[k] = v
        msg.set_content(mail.text)
        if mail.html:
            msg.add_alternative(mail.html, subtype="html")
        return msg

    def _smtp(self, msg: EmailMessage) -> None:
        s = self.s
        ctx = ssl.create_default_context()
        stage = "connect"  # how far we got decides the explanation: a hang-up at login means a bad password, not a bad port
        try:
            if s.smtp_security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=TIMEOUT_S, context=ctx)
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=TIMEOUT_S)
            with server:
                if s.smtp_security == "starttls":
                    stage = "starttls"
                    server.starttls(context=ctx)
                if s.smtp_user:
                    stage = "login"
                    server.login(s.smtp_user, _password(s))
                stage = "send"
                server.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise explain(exc, s, self.sender, stage) from exc

    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=TIMEOUT_S)

    def _post(self, url: str, payload: dict, headers: dict) -> httpx.Response:
        client = self._client()
        try:
            return client.post(url, json=payload, headers=headers)
        finally:
            if client is not self._http:
                client.close()

    def _api_brevo(self, mail: Email) -> None:
        payload: dict = {"sender": {"name": BRAND, "email": self.sender}, "to": [{"email": mail.to}], "subject": mail.subject,
                         "htmlContent": mail.html or _text_html(mail.text), "textContent": mail.text}
        if mail.reply_to:
            payload["replyTo"] = {"email": mail.reply_to}
        if mail.headers:
            payload["headers"] = mail.headers
        r = self._post(BREVO_URL, payload, {"api-key": self.s.brevo_api_key or "", "accept": "application/json"})
        if r.status_code >= 300:
            raise explain_api("Brevo", r, self.sender)

    def _api_resend(self, mail: Email) -> None:
        payload: dict = {"from": formataddr((BRAND, self.sender)), "to": [mail.to], "subject": mail.subject, "text": mail.text,
                         "html": mail.html or _text_html(mail.text)}
        if mail.reply_to:
            payload["reply_to"] = mail.reply_to
        if mail.headers:
            payload["headers"] = mail.headers
        r = self._post(RESEND_URL, payload, {"Authorization": f"Bearer {self.s.resend_api_key or ''}"})
        if r.status_code >= 300:
            raise explain_api("Resend", r, self.sender)

    def _write_outbox(self, msg: EmailMessage, subject: str) -> None:
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + "".join(ch if ch.isalnum() else "-" for ch in subject.lower())[:40] + ".eml"
            (self.outbox / name).write_bytes(bytes(msg))
            log.info("email isn't set up; wrote '%s' to outbox/%s", subject, name)
        except OSError as exc:
            log.warning("couldn't write outbox: %s", exc)


# ---- explaining failures ---------------------------------------------------------------------
def _is_gmail(host: str | None) -> bool:
    return bool(host) and host.lower().rstrip(".").endswith(("gmail.com", "googlemail.com"))


def _password(s: Settings) -> str:
    pw = s.smtp_password or ""
    # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
    if _is_gmail(s.smtp_host) and len(pw.replace(" ", "")) == 16:
        return pw.replace(" ", "")
    return pw


def _smtp_reply(exc: smtplib.SMTPResponseException) -> str:
    msg = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    return f"{exc.smtp_code} {' '.join(msg.split())}"[:220]


def explain(exc: BaseException, s: Settings, sender: str, stage: str | None = None) -> MailError:
    """Turn an SMTP or network error into what to change, in plain words.

    `stage` is how far the conversation got: connect, starttls, login or send.
    """
    where = f"{s.smtp_host}:{s.smtp_port}"
    gmail = _is_gmail(s.smtp_host)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if gmail:
            return MailError("Gmail didn't accept the login. SMTP_USER must be your full Gmail address and SMTP_PASSWORD a "
                             "16-letter App Password (Google Account → Security → 2-Step Verification → App passwords), "
                             f"not your normal password. Gmail said: {_smtp_reply(exc)}")
        return MailError(f"{s.smtp_host} didn't accept SMTP_USER and SMTP_PASSWORD. Many providers use a separate SMTP "
                         f"login and key: copy both from the provider's SMTP settings page. Server said: {_smtp_reply(exc)}")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        hint = "your Gmail address" if gmail else "a sender you've verified with your provider"
        return MailError(f"The server won't send from {sender}. Set EMAIL_FROM to {hint}. Server said: {_smtp_reply(exc)}")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return MailError("The server refused the recipient's address; check it for typos.", service=False)
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return MailError(f"{where} doesn't offer STARTTLS. Use SMTP_PORT=465 with SMTP_SECURITY=ssl.")
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        if stage == "login":
            if gmail:
                return MailError("Gmail hung up during sign-in. Usually SMTP_PASSWORD isn't a valid App Password "
                                 "(Google Account → Security → 2-Step Verification → App passwords) or SMTP_USER isn't "
                                 "your full Gmail address.")
            return MailError(f"{s.smtp_host} hung up during sign-in. Usually SMTP_USER or SMTP_PASSWORD is wrong: copy both "
                             "from the provider's SMTP settings page.")
        if stage == "send":
            return MailError(f"{s.smtp_host} hung up while taking the message. The provider may be refusing it: a daily "
                             f"sending limit reached, or {sender} isn't an allowed sender.")
        return MailError(f"{where} closed the connection. Usually the port and SMTP_SECURITY don't match: "
                         "587 needs starttls, 465 needs ssl.")
    if isinstance(exc, smtplib.SMTPResponseException):
        return MailError(f"{s.smtp_host} refused the message. Server said: {_smtp_reply(exc)}")
    if isinstance(exc, smtplib.SMTPException):
        return MailError(f"Sending through {where} failed: {exc}")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return MailError(f"{where}'s security certificate couldn't be checked ({getattr(exc, 'verify_message', None) or exc}). "
                         "Set SMTP_HOST to the provider's exact server name (e.g. smtp.gmail.com), not an IP address.")
    if isinstance(exc, ssl.SSLError):
        return MailError(f"The secure connection to {where} failed ({getattr(exc, 'reason', None) or exc}). Port 465 needs "
                         "SMTP_SECURITY=ssl; port 587 needs starttls.")
    if isinstance(exc, socket.gaierror):
        return MailError(f"Couldn't find the mail server '{s.smtp_host}'. Check SMTP_HOST for typos.")
    if isinstance(exc, httpx.TimeoutException):
        return MailError(f"The email service didn't answer within {TIMEOUT_S} seconds. Try again in a minute.")
    if isinstance(exc, TimeoutError):
        return MailError(f"No answer from {where} within {TIMEOUT_S} seconds. Some hosts block outgoing mail ports: "
                         + ("try SMTP_PORT=465 (SMTP_SECURITY=ssl), or " if s.smtp_port != 465 else "")
                         + "send over HTTPS instead with BREVO_API_KEY.")
    if isinstance(exc, ConnectionRefusedError):
        return MailError(f"{where} refused the connection. Check SMTP_PORT (usually 587, or 465 with SMTP_SECURITY=ssl).")
    if isinstance(exc, httpx.HTTPError):
        return MailError(f"Couldn't reach the email service: {exc}")
    return MailError(f"Couldn't connect to {where}: {exc}. If this server blocks mail ports, send over HTTPS with BREVO_API_KEY.")


def explain_api(provider: str, r: httpx.Response, sender: str) -> MailError:
    try:
        data = r.json()
        detail = str(data.get("message") or data.get("error") or data)
    except ValueError:
        detail = r.text
    detail = " ".join(detail.split())[:220]
    low = detail.lower()
    key = "BREVO_API_KEY" if provider == "Brevo" else "RESEND_API_KEY"
    if "not yet activated" in low or "account is not activated" in low:
        return MailError(f"{provider} hasn't switched on sending for your account yet. Ask {provider} support to activate "
                         f"transactional email. {provider} said: {detail}")
    if r.status_code in (401, 403):
        if "ip" in low.split() or "ip address" in low:
            return MailError(f"{provider} blocked this server's IP address. In Brevo: Security → Authorised IPs, add this "
                             f"server's address or turn the IP check off. {provider} said: {detail}")
        if provider == "Resend" and ("domain" in low or "testing emails" in low):
            return MailError(f"Resend only sends to other people once your domain is verified (Resend → Domains), and "
                             f"EMAIL_FROM must use that domain. Resend said: {detail}")
        return MailError(f"{provider} didn't accept {key}. Create a new API key in your {provider} account and paste it "
                         f"exactly. {provider} said: {detail}")
    if r.status_code == 429:
        return MailError(f"{provider}'s sending limit was hit (the free plan has a daily cap). {provider} said: {detail}")
    if "sender" in low or "from" in low.split() or "domain" in low:
        return MailError(f"{provider} won't send from {sender}. Add and verify it as a sender in {provider} (or verify its "
                         f"domain), then set EMAIL_FROM to it. {provider} said: {detail}")
    if r.status_code == 400 and ("to" in low.split() or "recipient" in low or "email is not valid" in low):
        return MailError(f"{provider} refused the recipient's address. {provider} said: {detail}", service=False)
    return MailError(f"{provider} answered {r.status_code}: {detail}")


# ---- templates ---------------------------------------------------------------------------
def _html(title: str, paragraphs: list[str], button: tuple[str, str] | None = None, footer: str = "") -> str:
    body = "".join(f"<p style='margin:0 0 14px'>{p}</p>" for p in paragraphs)
    btn = (f"<p style='margin:22px 0'><a href='{escape(button[1])}' style='background:#2b44c9;color:#fff;padding:11px 18px;"
           f"border-radius:8px;text-decoration:none;font-weight:600'>{escape(button[0])}</a></p>") if button else ""
    foot = f"<p style='color:#777;font-size:12px;margin-top:28px'>{footer}</p>" if footer else ""
    return (f"<div style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:520px;margin:auto;color:#1c2235;"
            f"font-size:15px;line-height:1.5'><p style='font-weight:700;font-size:18px'>♜ {BRAND}</p>"
            f"<h2 style='font-size:20px'>{escape(title)}</h2>{body}{btn}{foot}</div>")


def _text_html(text: str) -> str:
    return ("<div style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:15px;line-height:1.5;"
            f"white-space:pre-wrap'>{escape(text)}</div>")


def verification_email(link: str) -> tuple[str, str, str]:
    text = (f"Welcome to {BRAND}!\n\nConfirm your email address so we can reach you about your account:\n{link}\n\n"
            "The link works for 48 hours. If you didn't sign up, ignore this email.")
    html = _html("Confirm your email", ["Welcome! Confirm your email address so we can reach you about your account."],
                 ("Confirm email", link), "The link works for 48 hours. If you didn't sign up, ignore this email.")
    return "Confirm your email", text, html


def reset_email(link: str) -> tuple[str, str, str]:
    text = (f"Someone (hopefully you) asked to reset the password for your {BRAND} account.\n\nChoose a new password:\n{link}\n\n"
            "The link works for one hour and only once. If you didn't ask, ignore this email: your password hasn't changed.")
    html = _html("Reset your password", [f"Someone (hopefully you) asked to reset the password for your {BRAND} account."],
                 ("Choose a new password", link),
                 "The link works for one hour and only once. If you didn't ask, ignore this email: your password hasn't changed.")
    return "Reset your password", text, html


def coach_invite_email(coach_email: str, student_name: str, link: str) -> tuple[str, str, str]:
    text = (f"Hi {student_name},\n\nYour coach ({coach_email}) has added you to their squad on {BRAND}, where they follow your games, "
            f"your Rating DNA and your homework.\n\nJoin with this link to get Pro free while you're in the squad:\n{link}\n\n"
            "The link works for 14 days. If you weren't expecting this, ignore this email.")
    html = _html(f"{coach_email} invited you", [f"Hi {escape(student_name)},",
                 f"Your coach ({escape(coach_email)}) has added you to their squad on {BRAND}, where they follow your games, "
                 "your Rating DNA and your homework.", "Join to get Pro free while you're in the squad."],
                 ("Join the squad", link), "The link works for 14 days. If you weren't expecting this, ignore this email.")
    return f"Your coach invited you to {BRAND}", text, html


def support_email(from_email: str, subject: str, message: str, context: str) -> tuple[str, str]:
    return f"[Support] {subject}", f"From: {from_email}\n{context}\n\n{message}"


def _report(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass
