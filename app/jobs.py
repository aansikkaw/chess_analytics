"""A small persistent job queue for engine work (imports, Prep Checks, instant previews).

Why not just run imports in a background thread? Because one player importing 300 games
would hold a Stockfish process for half an hour while everyone else waits. The queue:

* **Persists jobs in SQLite**, so a restart doesn't lose them (running jobs are re-queued).
* **Is fair**: a user never has two jobs running at once, and long imports run in chunks
  (IMPORT_CHUNK games at a time) and go to the back of the line between chunks.
* **Shows results early**: the newest games are analysed first and the knowledge bundle is
  rebuilt after the first chunk, so the dashboard fills in while the rest imports.
* **Has a fast lane**: one worker only takes previews, so the landing page's
  "type a username" preview isn't stuck behind imports.
* **Can run anywhere**: in the web process (`JOB_MODE=thread`, the default), in a separate
  process (`JOB_MODE=external` + `python -m app.worker`), or inline for tests (`JOB_MODE=inline`).
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger("plateau.jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    user_id     INTEGER,
    account_id  INTEGER,
    payload     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',
    stage       TEXT NOT NULL DEFAULT 'Queued',
    done        INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    priority    INTEGER NOT NULL DEFAULT 5,
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  REAL,
    worker      TEXT,
    heartbeat   REAL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs (state, priority, created_at);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs (account_id, state);
"""

# Lower runs first.
PRIORITY = {"preview": 1, "prep": 3, "import": 5, "autosync": 8}
STALE_AFTER_S = 300  # a running job with no heartbeat for this long is assumed dead and re-queued
KEEP_FINISHED_DAYS = 14
ACTIVE = ("queued", "running")


class Requeue(Exception):
    """Raised by a handler to put its job back in the queue (next chunk, or a polite retry)."""

    def __init__(self, payload: dict | None = None, delay: float = 0.0, stage: str | None = None):
        super().__init__("requeue")
        self.payload, self.delay, self.stage = payload, delay, stage


class JobError(Exception):
    """A failure the user should see as-is (bad username, unreadable file, ...)."""


class JobQueue:
    def __init__(self, db_path: str):
        self.path = db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)  # autocommit; explicit BEGIN where needed
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- producers ----------------------------------------------------------
    def enqueue(self, kind: str, payload: dict | None = None, user_id: int | None = None,
                account_id: int | None = None, priority: int | None = None) -> str:
        job_id = secrets.token_urlsafe(12)
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, kind, user_id, account_id, payload, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, kind, user_id, account_id, json.dumps(payload or {}), PRIORITY.get(kind, 5) if priority is None else priority,
                 time.time()),
            )
        return job_id

    def enqueue_for_account(self, kind: str, payload: dict, user_id: int, account_id: int, priority: int | None = None) -> str | None:
        """Like enqueue, but atomically refuses (returns None) if the account already has a queued or running job."""
        job_id = secrets.token_urlsafe(12)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                busy = c.execute("SELECT 1 FROM jobs WHERE account_id = ? AND state IN ('queued', 'running') LIMIT 1", (account_id,)).fetchone()
                if busy:
                    c.execute("COMMIT")
                    return None
                c.execute("INSERT INTO jobs (id, kind, user_id, account_id, payload, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (job_id, kind, user_id, account_id, json.dumps(payload or {}), PRIORITY.get(kind, 5) if priority is None else priority,
                           time.time()))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return job_id

    def cancel_for_account(self, account_id: int) -> None:
        """Drop queued jobs for an account being removed; a running one notices and stops at its next chunk."""
        with self._conn() as c:
            c.execute("UPDATE jobs SET state = 'error', error = 'This account was removed.', finished_at = ?, payload = '{}' "
                      "WHERE account_id = ? AND state IN ('queued', 'running')", (time.time(), account_id))

    def active_for_account(self, account_id: int) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT id FROM jobs WHERE account_id = ? AND state IN ('queued', 'running') ORDER BY created_at LIMIT 1",
                          (account_id,)).fetchone()
        return r["id"] if r else None

    # ---- reading --------------------------------------------------------------
    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def view(self, job_id: str, user_id: int | None) -> dict | None:
        """What the browser sees: progress plus the result's summary fields, never the payload.

        Polled every second or so, so it never reads the payload column (which can hold a big PGN)."""
        with self._conn() as c:
            r = c.execute("SELECT id, kind, user_id, state, stage, done, total, skipped, error, result, priority, created_at "
                          "FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not r or r["user_id"] != user_id:
            return None
        j = dict(r)
        j["result"] = json.loads(j["result"]) if j["result"] else None
        out = {k: j[k] for k in ("id", "kind", "state", "stage", "done", "total", "skipped", "error")}
        out["position"] = self.position(j) if j["state"] == "queued" else 0
        out.update(j["result"] or {})
        return out

    def position(self, job: dict) -> int:
        """How many jobs will start before this one (roughly)."""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND (priority < ? OR (priority = ? AND created_at < ?))",
                (job["priority"], job["priority"], job["created_at"]),
            ).fetchone()[0]

    def stats(self) -> dict:
        with self._conn() as c:
            counts = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
            oldest = c.execute("SELECT MIN(created_at) FROM jobs WHERE state = 'queued'").fetchone()[0]
            beat = c.execute("SELECT MAX(heartbeat) FROM jobs").fetchone()[0]
            day_ago = time.time() - 86_400
            failed = c.execute("SELECT COUNT(*) FROM jobs WHERE state = 'error' AND finished_at > ?", (day_ago,)).fetchone()[0]
            finished = c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('done', 'error') AND finished_at > ?", (day_ago,)).fetchone()[0]
        return {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "oldest_wait_s": round(time.time() - oldest) if oldest else 0,
                "last_heartbeat_s": round(time.time() - beat) if beat else None,
                "finished_24h": finished, "failed_24h": failed}

    # ---- workers ----------------------------------------------------------------
    def claim(self, worker: str, kinds: tuple[str, ...] | None = None, exclude: tuple[str, ...] = ()) -> dict | None:
        """Atomically take the next job. A user with a running job waits, so one person can't fill every worker."""
        now = time.time()
        kind_sql = f" AND j.kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        if exclude:
            kind_sql += f" AND j.kind NOT IN ({','.join('?' * len(exclude))})"
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute(
                    "SELECT * FROM jobs j WHERE j.state = 'queued' AND (j.not_before IS NULL OR j.not_before <= ?)" + kind_sql +
                    " AND (j.user_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs r WHERE r.state = 'running' AND r.user_id = j.user_id))"
                    " ORDER BY j.priority, j.created_at LIMIT 1",
                    (now, *(kinds or ()), *exclude),
                ).fetchone()
                if r is None:
                    c.execute("COMMIT")
                    return None
                c.execute("UPDATE jobs SET state = 'running', worker = ?, heartbeat = ?, started_at = COALESCE(started_at, ?), "
                          "attempts = attempts + 1 WHERE id = ?", (worker, now, now, r["id"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        job = self._row(r)
        job["state"] = "running"
        return job

    def progress(self, job_id: str, **fields) -> None:
        allowed = {"stage", "done", "total", "skipped"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET heartbeat = ?{''.join(f', {k} = ?' for k in sets)} WHERE id = ?",  # keys whitelisted
                      (time.time(), *sets.values(), job_id))

    def merge_result(self, job_id: str, **result) -> None:
        """Add fields to the job's result while it's still running (e.g. 'first results ready')."""
        j = self.get(job_id)
        if j:
            with self._conn() as c:
                c.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps({**(j["result"] or {}), **result}), job_id))

    def finish(self, job_id: str, result: dict | None = None, stage: str = "Done") -> None:
        j = self.get(job_id)
        merged = {**((j or {}).get("result") or {}), **(result or {})}
        with self._conn() as c:
            c.execute("UPDATE jobs SET state = 'done', stage = ?, result = ?, finished_at = ?, heartbeat = ? WHERE id = ?",
                      (stage, json.dumps(merged), time.time(), time.time(), job_id))

    def fail(self, job_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET state = 'error', error = ?, finished_at = ?, heartbeat = ? WHERE id = ?",
                      (error[:1000], time.time(), time.time(), job_id))

    def requeue(self, job_id: str, payload: dict | None = None, delay: float = 0.0, stage: str | None = None) -> None:
        """Back of the line (created_at = now), optionally not before `delay` seconds."""
        now = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET state = 'queued', worker = NULL, created_at = ?, not_before = ?, "
                "payload = COALESCE(?, payload), stage = COALESCE(?, stage) WHERE id = ?",
                (now, now + delay if delay else None, json.dumps(payload) if payload is not None else None, stage, job_id),
            )

    def recover(self, stale_after: float = STALE_AFTER_S) -> int:
        """Re-queue running jobs whose worker stopped sending heartbeats (crash, deploy, restart)."""
        with self._conn() as c:
            cur = c.execute("UPDATE jobs SET state = 'queued', worker = NULL, stage = 'Resuming after a restart' "
                            "WHERE state = 'running' AND (heartbeat IS NULL OR heartbeat < ?)", (time.time() - stale_after,))
            return cur.rowcount

    def prune(self, days: int = KEEP_FINISHED_DAYS) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM jobs WHERE state IN ('done', 'error') AND finished_at < ?", (time.time() - days * 86_400,))
            return cur.rowcount

    def find_recent(self, kind: str, key: str, within_s: float) -> dict | None:
        """A finished job of this kind with payload.key == key, newer than within_s (used to cache previews)."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE kind = ? AND created_at > ? ORDER BY created_at DESC LIMIT 50",
                             (kind, time.time() - within_s)).fetchall()
        for r in rows:
            j = self._row(r)
            if j["payload"].get("key") == key and j["state"] in ("queued", "running", "done"):
                return j
        return None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d


Handler = Callable[[dict, "WorkerContext"], dict | None]


class WorkerContext:
    """What a handler gets besides the job: progress reporting and its worker's engines."""

    def __init__(self, queue: JobQueue, job: dict, resources: dict):
        self.queue, self.job, self.resources = queue, job, resources

    def progress(self, **fields) -> None:
        self.queue.progress(self.job["id"], **fields)

    def partial(self, **result) -> None:
        self.queue.merge_result(self.job["id"], **result)


def run_one(queue: JobQueue, job: dict, handlers: dict[str, Handler], resources: dict) -> None:
    """Run a claimed job through its handler and record the outcome. Never raises."""
    handler = handlers.get(job["kind"])
    if handler is None:
        queue.fail(job["id"], f"No handler for job kind '{job['kind']}'.")
        return
    try:
        result = handler(job, WorkerContext(queue, job, resources))
        queue.finish(job["id"], result)
    except Requeue as rq:
        queue.requeue(job["id"], rq.payload, rq.delay, rq.stage)
    except JobError as exc:
        queue.fail(job["id"], str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad job must never kill a worker
        log.exception("job %s (%s) failed", job["id"], job["kind"])
        _report(exc)
        queue.fail(job["id"], f"Something went wrong on our side ({type(exc).__name__}). We've been notified; please try again.")


def drain(queue: JobQueue, handlers: dict[str, Handler], resources: dict, job_id: str, max_steps: int = 500) -> None:
    """Inline mode: run one job (and its re-queued chunks) to completion right now."""
    for _ in range(max_steps):
        j = queue.get(job_id)
        if not j or j["state"] not in ACTIVE:
            return
        with queue._conn() as c:
            c.execute("UPDATE jobs SET state = 'running', attempts = attempts + 1, not_before = NULL, heartbeat = ? WHERE id = ?",
                      (time.time(), job_id))
        run_one(queue, {**j, "state": "running"}, handlers, resources)


class WorkerPool:
    """N general worker threads plus one fast-lane thread for previews."""

    def __init__(self, queue: JobQueue, handlers: dict[str, Handler], make_resources: Callable[[], dict],
                 workers: int = 2, fast_lane: tuple[str, ...] = ("preview",), name: str = "web", poll_s: float = 1.0):
        self.queue, self.handlers, self.make_resources = queue, handlers, make_resources
        self.poll_s = poll_s
        self._stop = threading.Event()
        # General workers leave fast-lane kinds alone, so each thread only ever starts one Stockfish.
        lanes = [(None, tuple(fast_lane), f"{name}-{i}") for i in range(max(1, workers))]
        if fast_lane:
            lanes.append((tuple(fast_lane), (), f"{name}-fast"))
        self.threads = [threading.Thread(target=self._loop, args=(kinds, excl, wid), daemon=True, name=wid) for kinds, excl, wid in lanes]
        self._last_maintenance = 0.0

    def start(self) -> None:
        self.queue.recover()
        for t in self.threads:
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self.threads:
            t.join(timeout)

    def alive(self) -> int:
        return sum(t.is_alive() for t in self.threads)

    def _loop(self, kinds: tuple[str, ...] | None, exclude: tuple[str, ...], wid: str) -> None:
        resources = self.make_resources()
        try:
            while not self._stop.is_set():
                self._maintenance()
                try:
                    job = self.queue.claim(wid, kinds, exclude)
                except sqlite3.OperationalError:  # database briefly locked; try again shortly
                    job = None
                if job is None:
                    self._stop.wait(self.poll_s)
                    continue
                run_one(self.queue, job, self.handlers, resources)
        finally:
            for r in resources.values():
                close = getattr(r, "close", None)
                if close:
                    close()

    def _maintenance(self) -> None:
        now = time.time()
        if now - self._last_maintenance > 60:
            self._last_maintenance = now
            try:
                self.queue.recover()
                self.queue.prune()
            except sqlite3.OperationalError:
                pass


def _report(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass
