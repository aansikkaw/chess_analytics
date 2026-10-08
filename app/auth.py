"""Email + password accounts with server-side sessions.

* Passwords: scrypt (memory-hard, in Python's standard library) with a per-user salt.
* Sessions: a random 256-bit token in an HttpOnly cookie; only its SHA-256 hash is
  stored, so a leaked database doesn't leak live sessions.
* Brute force: a simple per-IP limit on login attempts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

SESSION_COOKIE = "pb_session"
SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
MIN_PASSWORD = 8

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


class AuthError(Exception):
    pass


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    return email


def check_password_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters for your password.")
    if len(password) > 200:
        raise AuthError("That password is too long.")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def new_session_token() -> tuple[str, str, float]:
    """(token for the cookie, hash for the database, expiry timestamp)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token), time.time() + SESSION_DAYS * 86_400


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginLimiter:
    """At most `limit` failed logins per IP per `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 600):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            q = self._hits[ip]
            while q and time.time() - q[0] > self.window:
                q.popleft()
            return len(q) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self._hits[ip].append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)
