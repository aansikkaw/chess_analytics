"""The public status page's data: is each part of the service working right now?

/api/health stays tiny and fast for uptime monitors (UptimeRobot, Better Stack, ...).
/api/status (and the /status page) checks every dependency, caching the outside ones
(Lichess, Chess.com) for a few minutes so the page can't be used to hammer them.
"""

from __future__ import annotations

import threading
import time

import httpx

from .config import Settings
from .sources import USER_AGENT

EXTERNAL_TTL_S = 300
BACKUP_MAX_AGE_H = 36
_started = time.time()


class CoachStats:
    """Rolling record of recent coach answers: did the AI model answer, or did we fall back?"""

    def __init__(self, keep: int = 20):
        self.keep, self.results, self.last_error = keep, [], None
        self._lock = threading.Lock()

    def record(self, mode: str, notice: str | None) -> None:
        with self._lock:
            self.results = (self.results + [mode])[-self.keep:]
            if mode == "fallback":
                self.last_error = {"at": time.time(), "notice": notice}

    def summary(self) -> dict:
        with self._lock:
            agent = sum(m == "agent" for m in self.results)
            fallback = sum(m == "fallback" for m in self.results)
        return {"recent_ai_answers": agent, "recent_fallbacks": fallback, "last_fallback": self.last_error}


class StatusChecker:
    def __init__(self, settings: Settings, store, jobs, mailer, coach_stats: CoachStats, engine_state: dict, backends_fn):
        self.s, self.store, self.jobs, self.mailer = settings, store, jobs, mailer
        self.coach_stats, self.engine_state, self.backends_fn = coach_stats, engine_state, backends_fn
        self._ext: dict = {}
        self._ext_at = 0.0
        self._lock = threading.Lock()

    # ---- outside services, cached --------------------------------------------------------
    def external(self, http: httpx.Client | None = None) -> dict:
        with self._lock:
            if self._ext and time.time() - self._ext_at < EXTERNAL_TTL_S:
                return self._ext
            client = http or httpx.Client(timeout=5.0, headers={"User-Agent": USER_AGENT})
            try:
                out = {"lichess": _probe(client, "https://lichess.org/api/users/status?ids=lichess")}
                if self.s.chesscom_enabled:
                    out["chesscom"] = _probe(client, "https://api.chess.com/pub/player/hikaru")
            finally:
                if http is None:
                    client.close()
            self._ext, self._ext_at = out, time.time()
            return out

    # ---- everything ------------------------------------------------------------------------
    def report(self, include_external: bool = True, http: httpx.Client | None = None) -> dict:
        checks: dict[str, dict] = {}
        try:
            checks["database"] = {"ok": True, "latency_ms": round(self.store.ping(), 1)}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"ok": False, "detail": type(exc).__name__}

        engine_ok = self.engine_state.get("engine") is not None
        checks["engine"] = {"ok": engine_ok, "detail": None if engine_ok else (self.engine_state.get("engine_error") or "Stockfish not running")}

        q = self.jobs.queue.stats() if checks["database"]["ok"] else {}
        alive = self.jobs.workers_alive()
        if alive is None:  # workers run in another process: judge by heartbeat while work is waiting
            workers_ok = not q.get("queued") or (q.get("last_heartbeat_s") is not None and q["last_heartbeat_s"] < 120)
        else:
            workers_ok = alive > 0 or self.jobs.mode == "inline"
        backlog_ok = q.get("oldest_wait_s", 0) < 900
        checks["imports"] = {"ok": bool(workers_ok and backlog_ok), "workers": alive, "mode": self.jobs.mode, **q}

        backends = self.backends_fn()
        coach = self.coach_stats.summary()
        coach_ok = not (coach["recent_fallbacks"] >= 3 and coach["recent_ai_answers"] == 0)
        checks["coach"] = {"ok": coach_ok, "providers": [b.label for b in backends] or ["built-in (offline) coach"], **coach}

        last = self.store.kv_get("last_backup") if checks["database"]["ok"] else None
        target_set = bool(_env("BACKUP_TARGET"))
        if last:
            age_h = (time.time() - last["updated_at"]) / 3600
            checks["backups"] = {"ok": last.get("ok", False) and age_h < BACKUP_MAX_AGE_H, "last_at": last["updated_at"],
                                 "age_hours": round(age_h, 1), "configured": target_set}
            rt = self.store.kv_get("last_restore_test")
            if rt:
                checks["backups"]["last_restore_test_at"] = rt["updated_at"]
                checks["backups"]["last_restore_test_ok"] = rt.get("ok", False)
                if not rt.get("ok", False):
                    checks["backups"]["ok"] = False
                    checks["backups"]["detail"] = "The last restore test failed: check the scheduler logs."
        else:
            checks["backups"] = {"ok": not target_set, "configured": target_set,
                                 "detail": "No backup has run yet." if target_set else "Backups aren't configured on this server."}

        checks["email"] = self.email_check() if checks["database"]["ok"] else {"ok": True, "configured": self.mailer.configured}
        if include_external:
            for name, r in self.external(http).items():
                checks[name] = r

        core = ("database",)
        important = ("engine", "imports", "coach", "backups", "email", "lichess", "chesscom")
        if not all(checks[k]["ok"] for k in core if k in checks):
            overall = "down"
        elif all(checks[k]["ok"] for k in important if k in checks):
            overall = "ok"
        else:
            overall = "degraded"
        return {"status": overall, "version": self.s.app_version, "uptime_s": round(time.time() - _started),
                "checked_at": time.time(), "support_email": self.s.support_email, "checks": checks}


    def email_check(self) -> dict:
        """Judged by what actually happened: the last email sent (by any process) went out or didn't.

        Public, so it never shows the provider's error; the owner sees that in the account panel
        and from `scripts/admin.py test-email`.
        """
        if not self.mailer.configured:
            return {"ok": True, "configured": False}
        last = self.mailer.last_result()
        out = {"ok": True, "configured": True, "last_ok_at": (last or {}).get("last_ok_at")}
        if last and not last.get("ok"):
            out.update(ok=False, failed_at=last.get("at"),
                       detail="Emails aren't going out right now, so verification and reset links may not arrive.")
        return out


def _probe(client: httpx.Client, url: str) -> dict:
    t = time.perf_counter()
    try:
        r = client.get(url)
        ms = round((time.perf_counter() - t) * 1000)
        return {"ok": r.status_code < 500 and r.status_code != 429, "latency_ms": ms, "http": r.status_code}
    except httpx.TimeoutException:
        return {"ok": False, "detail": "Timed out: the site didn't answer within 5 seconds."}
    except httpx.HTTPError:
        return {"ok": False, "detail": "Couldn't connect to the site from our server."}


def _env(name: str) -> str:
    import os

    return os.getenv(name, "")
