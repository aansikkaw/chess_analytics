"""SQLite persistence: users, linked chess accounts, analysed games, the puzzle
deck (with spaced repetition), daily usage counters and upgrade requests.

One connection per call keeps this safe to use from FastAPI's worker threads;
WAL mode lets the background sync write while requests read.

Games and puzzles are keyed by a *player key* ("u<user_id>:<platform>:<handle>"),
so two users who link the same chess account never see each other's data.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Iterator

from .analysis import GameAnalysis, MoveAnalysis

# Leitner boxes: a correct answer moves the puzzle up a box, which pushes the
# next review further out. A wrong answer sends it back to box 1.
BOX_INTERVAL_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
DAY = 86_400

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    username TEXT NOT NULL,
    game_id  TEXT NOT NULL,
    data     TEXT NOT NULL,
    PRIMARY KEY (username, game_id)
);
CREATE TABLE IF NOT EXISTS puzzles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL,
    game_id      TEXT NOT NULL,
    ply          INTEGER NOT NULL,
    fen          TEXT NOT NULL,
    solution     TEXT NOT NULL,
    solution_san TEXT NOT NULL,
    line         TEXT NOT NULL,
    played_san   TEXT NOT NULL,
    themes       TEXT NOT NULL,
    phase        TEXT NOT NULL,
    win_loss     REAL NOT NULL,
    box          INTEGER NOT NULL DEFAULT 1,
    due_at       REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    solved       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (username, game_id, ply)
);
CREATE INDEX IF NOT EXISTS puzzles_due ON puzzles (username, due_at);
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    platform        TEXT NOT NULL,
    handle          TEXT NOT NULL,
    auto_sync       INTEGER NOT NULL DEFAULT 0,
    time_classes    TEXT NOT NULL DEFAULT 'blitz,rapid,classical',
    last_synced_at  REAL,
    last_sync_error TEXT,
    created_at      REAL NOT NULL,
    UNIQUE (user_id, platform, handle)
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);
CREATE TABLE IF NOT EXISTS repertoires (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    account_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL,
    pgn         TEXT NOT NULL,
    report      TEXT,
    report_at   REAL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS upgrade_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    note       TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    purpose     TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS support_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    email      TEXT NOT NULL,
    subject    TEXT NOT NULL,
    message    TEXT NOT NULL,
    context    TEXT,
    emailed    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    coach_id         INTEGER NOT NULL,
    account_id       INTEGER NOT NULL,  -- a chess account owned by the coach (role 'student')
    name             TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    student_user_id  INTEGER,           -- set when the student accepts the coach's invite
    invite_hash      TEXT,
    invite_expires   REAL,
    joined_at        REAL,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS students_coach ON students (coach_id);
CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL,
    coach_id    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    skill       TEXT,
    due_at      REAL,
    done_at     REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assignments_student ON assignments (student_id);
CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

USER_COLUMNS = {  # added after v0.4; created by _migrate on older databases
    "email_verified_at": "REAL",
    "plan_expires_at": "REAL",  # set for time-limited plans such as the Event Pass
    "digest_opt_in": "INTEGER NOT NULL DEFAULT 1",
    "last_digest_at": "REAL",
    "unsubscribe_token": "TEXT",
}


class Store:
    def __init__(self, path: str):
        self.path = path
        self._games_cache: dict[str, tuple[tuple, list]] = {}
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Add columns introduced after a database was created (safe to run every start)."""
        def cols(table):
            return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if "accept" not in cols("puzzles"):
            c.execute("ALTER TABLE puzzles ADD COLUMN accept TEXT NOT NULL DEFAULT '[]'")  # extra moves that count as correct
        if "kind" not in cols("puzzles"):
            c.execute("ALTER TABLE puzzles ADD COLUMN kind TEXT NOT NULL DEFAULT 'mistake'")  # 'mistake' or 'prep'
        if "role" not in cols("accounts"):
            c.execute("ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'own'")  # 'own' or 'student' (a coach's roster)
        if "time_classes" not in cols("accounts"):
            c.execute("ALTER TABLE accounts ADD COLUMN time_classes TEXT NOT NULL DEFAULT 'blitz,rapid,classical'")
        have = cols("users")
        for name, decl in USER_COLUMNS.items():
            if name not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")  # names/decls are constants above
        if "plan" not in cols("upgrade_requests"):
            c.execute("ALTER TABLE upgrade_requests ADD COLUMN plan TEXT NOT NULL DEFAULT 'pro'")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- games ---------------------------------------------------------
    def save_games(self, username: str, games: list[GameAnalysis]) -> None:
        user = username.lower()
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO games (username, game_id, data) VALUES (?, ?, ?)",
                [(user, g.game_id, json.dumps(g.to_dict())) for g in games],
            )

    def known_game_ids(self, username: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT game_id FROM games WHERE username = ?", (username.lower(),))
            return {r["game_id"] for r in rows}

    def load_games(self, username: str) -> list[GameAnalysis]:
        """All analysed games for a player. Cached per process and re-read when the games change
        (checked with a cheap count/rowid query, so a worker process's writes are seen at once)."""
        user = username.lower()
        with self._conn() as c:
            sig = tuple(c.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0), COALESCE(SUM(rowid), 0) FROM games WHERE username = ?",
                                  (user,)).fetchone())
            hit = self._games_cache.get(user)
            if hit and hit[0] == sig:
                return list(hit[1])
            rows = c.execute("SELECT data FROM games WHERE username = ?", (user,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["moves"] = [MoveAnalysis(**m) for m in d["moves"]]
            out.append(GameAnalysis(**d))
        if len(self._games_cache) >= 64:
            self._games_cache.pop(next(iter(self._games_cache)))
        self._games_cache[user] = (sig, out)
        return list(out)

    def games_meta(self, username: str, game_ids: set[str]) -> dict[str, dict]:
        """Just the fields a puzzle needs from its game (moves, opponent, date), without building full analyses."""
        if not game_ids:
            return {}
        ids = sorted(game_ids)[:200]
        with self._conn() as c:
            rows = c.execute(f"SELECT game_id, data FROM games WHERE username = ? AND game_id IN ({','.join('?' * len(ids))})",
                             [username.lower(), *ids]).fetchall()
        out = {}
        for r in rows:
            d = json.loads(r["data"])
            out[r["game_id"]] = {k: d.get(k) for k in ("moves_san", "opponent", "played_at", "player_color", "url", "time_class")}
        return out

    # ---- puzzles -------------------------------------------------------
    def add_puzzles_from(self, username: str, games: list[GameAnalysis]) -> int:
        """Every mistake or blunder with a known better move becomes a puzzle."""
        user, now, rows = username.lower(), time.time(), []
        for g in games:
            for m in g.moves:
                if m.classification not in ("mistake", "blunder") or not m.best or m.best == m.played:
                    continue
                rows.append((
                    user, g.game_id, m.ply, m.fen_before, m.best, m.best_san or m.best,
                    json.dumps(m.best_line), m.played_san, json.dumps(m.tags or [m.phase]),
                    m.phase, m.win_loss, now,
                ))
        with self._conn() as c:
            before = c.total_changes
            c.executemany(
                """INSERT OR IGNORE INTO puzzles
                   (username, game_id, ply, fen, solution, solution_san, line, played_san,
                    themes, phase, win_loss, due_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return c.total_changes - before

    def add_prep_drills(self, player_key: str, repertoire_id: int, drills: list[dict]) -> int:
        """'What does your prep say here?' puzzles. Re-uploading replaces this repertoire's drills."""
        user, now = player_key.lower(), time.time()
        gid = f"prep-{repertoire_id}"
        rows = []
        for i, d in enumerate(drills):
            weight = 25.0 if d.get("you_played") else min(10.0, float(d.get("reached", 0)))
            rows.append((user, gid, i, d["fen"], d["accept"][0], d["prep_moves"][0], json.dumps([]),
                         d.get("you_played") or "", json.dumps(["repertoire"]), "opening", weight, now,
                         json.dumps(d["accept"]), "prep"))
        with self._conn() as c:
            c.execute("DELETE FROM puzzles WHERE username = ? AND game_id = ?", (user, gid))
            c.executemany(
                """INSERT OR IGNORE INTO puzzles
                   (username, game_id, ply, fen, solution, solution_san, line, played_san, themes, phase, win_loss,
                    due_at, accept, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def delete_prep_drills(self, player_key: str, repertoire_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM puzzles WHERE username = ? AND game_id = ?", (player_key.lower(), f"prep-{repertoire_id}"))

    # ---- repertoires -----------------------------------------------------------
    def add_repertoire(self, user_id: int, account_id: int, name: str, color: str, pgn: str) -> int:
        with self._conn() as c:
            return c.execute(
                "INSERT INTO repertoires (user_id, account_id, name, color, pgn, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, account_id, name, color, pgn, time.time()),
            ).lastrowid

    def repertoires(self, account_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM repertoires WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["report"] = json.loads(d["report"]) if d["report"] else None
            out.append(d)
        return out

    def repertoire(self, rep_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM repertoires WHERE id = ?", (rep_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["report"] = json.loads(d["report"]) if d["report"] else None
        return d

    def save_repertoire_report(self, rep_id: int, report: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE repertoires SET report = ?, report_at = ? WHERE id = ?", (json.dumps(report), time.time(), rep_id))

    def delete_repertoire(self, rep_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM repertoires WHERE id = ?", (rep_id,))

    @staticmethod
    def _puzzle_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["line"] = json.loads(d["line"])
        d["themes"] = json.loads(d["themes"])
        d["accept"] = json.loads(d.get("accept") or "[]")
        return d

    def puzzles(self, username: str, due_only: bool = True, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM puzzles WHERE username = ?"
        args: list = [username.lower()]
        if due_only:
            sql += " AND due_at <= ?"
            args.append(time.time())
        # Biggest swings first: the most expensive mistakes are the most worth fixing.
        sql += " ORDER BY box ASC, win_loss DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [self._puzzle_row(r) for r in c.execute(sql, args)]

    def count_due(self, username: str) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM puzzles WHERE username = ? AND due_at <= ?",
                (username.lower(), time.time()),
            ).fetchone()[0]

    def get_puzzle(self, puzzle_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM puzzles WHERE id = ?", (puzzle_id,)).fetchone()
        return self._puzzle_row(r) if r else None

    def record_attempt(self, puzzle_id: int, correct: bool) -> dict:
        with self._conn() as c:
            r = c.execute("SELECT box FROM puzzles WHERE id = ?", (puzzle_id,)).fetchone()
            if r is None:
                raise KeyError(puzzle_id)
            box = min(r["box"] + 1, 5) if correct else 1
            # Wrong answers come back in 10 minutes; right ones per the box interval.
            due = time.time() + (BOX_INTERVAL_DAYS[box] * DAY if correct else 600)
            c.execute(
                "UPDATE puzzles SET box = ?, due_at = ?, attempts = attempts + 1, solved = solved + ? WHERE id = ?",
                (box, due, int(correct), puzzle_id),
            )
        return {"box": box, "next_review_in_days": round((due - time.time()) / DAY, 2)}

    def count_games(self, player_key: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM games WHERE username = ?", (player_key.lower(),)).fetchone()[0]

    def delete_player(self, player_key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM games WHERE username = ?", (player_key.lower(),))
            c.execute("DELETE FROM puzzles WHERE username = ?", (player_key.lower(),))

    # ---- users & sessions ------------------------------------------------
    def create_user(self, email: str, pw_hash: str, plan: str = "free", is_admin: bool = False) -> int:
        with self._conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO users (email, pw_hash, plan, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, pw_hash, plan, int(is_admin), time.time()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("An account with that email already exists.") from exc
            return cur.lastrowid

    def user_by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(r) if r else None

    def user_by_id(self, user_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(r) if r else None

    def set_plan(self, user_id: int, plan: str, expires_at: float | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?", (plan, expires_at, user_id))

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"digest_opt_in", "last_digest_at", "email_verified_at", "pw_hash"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            with self._conn() as c:
                c.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",  # keys whitelisted above
                          (*sets.values(), user_id))

    def unsubscribe_token(self, user_id: int) -> str:
        with self._conn() as c:
            r = c.execute("SELECT unsubscribe_token FROM users WHERE id = ?", (user_id,)).fetchone()
            if r and r["unsubscribe_token"]:
                return r["unsubscribe_token"]
            tok = secrets.token_urlsafe(24)
            c.execute("UPDATE users SET unsubscribe_token = ? WHERE id = ?", (tok, user_id))
            return tok

    def user_by_unsubscribe_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE unsubscribe_token = ?", (token,)).fetchone()
        return dict(r) if r else None

    def delete_user(self, user_id: int) -> list[dict]:
        """Delete a user and everything they own. Returns their linked accounts (so bundles can be removed too)."""
        accounts = self.accounts(user_id)
        with self._conn() as c:
            for a in accounts:
                key = f"u{a['user_id']}:{a['platform']}:{a['handle'].lower()}"
                c.execute("DELETE FROM games WHERE username = ?", (key,))
                c.execute("DELETE FROM puzzles WHERE username = ?", (key,))
            for table in ("accounts", "repertoires", "sessions", "usage", "upgrade_requests", "email_tokens"):
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))  # table names are constants
            c.execute("DELETE FROM support_messages WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM assignments WHERE coach_id = ?", (user_id,))
            c.execute("DELETE FROM students WHERE coach_id = ?", (user_id,))
            c.execute("UPDATE students SET student_user_id = NULL, joined_at = NULL WHERE student_user_id = ?", (user_id,))
            if c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone():
                c.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return accounts

    def delete_sessions(self, user_id: int, except_hash: str | None = None) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, except_hash or ""))

    # ---- one-time email tokens (verification, password reset) -----------------------------
    def create_email_token(self, user_id: int, purpose: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(32)
        with self._conn() as c:
            c.execute("DELETE FROM email_tokens WHERE expires_at < ?", (time.time() - 86_400,))
            if purpose == "reset":  # only the newest reset link works
                c.execute("DELETE FROM email_tokens WHERE user_id = ? AND purpose = 'reset'", (user_id,))
            c.execute("INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
                      (_sha(token), user_id, purpose, time.time() + ttl_s))
        return token

    def use_email_token(self, token: str, purpose: str) -> int | None:
        """The token's user id if it's valid, unexpired and unused; marks it used."""
        if not token:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM email_tokens WHERE token_hash = ? AND purpose = ?", (_sha(token), purpose)).fetchone()
            if not r or r["used_at"] or r["expires_at"] < time.time():
                return None
            c.execute("UPDATE email_tokens SET used_at = ? WHERE token_hash = ?", (time.time(), r["token_hash"]))
            return r["user_id"]

    # ---- support, key-value ------------------------------------------------------------------
    def add_support_message(self, user_id: int | None, email: str, subject: str, message: str, context: str, emailed: bool) -> int:
        with self._conn() as c:
            return c.execute(
                "INSERT INTO support_messages (user_id, email, subject, message, context, emailed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, subject, message, context, int(emailed), time.time()),
            ).lastrowid

    def support_messages(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM support_messages ORDER BY id DESC LIMIT ?", (limit,))]

    def kv_get(self, k: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT v, updated_at FROM kv WHERE k = ?", (k,)).fetchone()
        return {**json.loads(r["v"]), "updated_at": r["updated_at"]} if r else None

    def kv_set(self, k: str, v: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, "
                      "updated_at = excluded.updated_at", (k, json.dumps(v), time.time()))

    def digest_candidates(self, min_gap_s: float = 6.5 * 86_400) -> list[dict]:
        """Verified users who want the weekly email and haven't had one this week."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM users WHERE email_verified_at IS NOT NULL AND digest_opt_in = 1 "
                "AND (last_digest_at IS NULL OR last_digest_at < ?) ORDER BY COALESCE(last_digest_at, 0), id",
                (time.time() - min_gap_s,))]

    def ping(self) -> float:
        """Round-trip a trivial query; returns milliseconds."""
        t = time.perf_counter()
        with self._conn() as c:
            c.execute("SELECT 1").fetchone()
        return (time.perf_counter() - t) * 1000

    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash, user_id, expires_at))

    def session_user(self, token_hash: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
                (token_hash, time.time()),
            ).fetchone()
        return dict(r) if r else None

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # ---- linked chess accounts ----------------------------------------------
    def add_account(self, user_id: int, platform: str, handle: str, role: str = "own") -> dict:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO accounts (user_id, platform, handle, created_at, role) VALUES (?, ?, ?, ?, ?)",
                (user_id, platform, handle, time.time(), role),
            )
            r = c.execute(
                "SELECT * FROM accounts WHERE user_id = ? AND platform = ? AND handle = ?", (user_id, platform, handle)
            ).fetchone()
        return dict(r)

    def accounts(self, user_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM accounts WHERE user_id = ? ORDER BY id", (user_id,))]

    def account(self, account_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(r) if r else None

    def delete_account(self, account_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            c.execute("DELETE FROM repertoires WHERE account_id = ?", (account_id,))
            if c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone():
                c.execute("UPDATE jobs SET state = 'error', error = 'This account was removed.', payload = '{}' "
                          "WHERE account_id = ? AND state IN ('queued', 'running')", (account_id,))

    def update_account(self, account_id: int, **fields) -> None:
        allowed = {"auto_sync", "last_synced_at", "last_sync_error", "time_classes"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE accounts SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",  # keys are whitelisted above
                (*sets.values(), account_id),
            )

    def auto_sync_accounts(self) -> list[dict]:
        """Accounts with auto-sync on whose owner is on the Pro plan."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT a.* FROM accounts a JOIN users u ON u.id = a.user_id "
                "WHERE a.auto_sync = 1 AND u.plan = 'pro' AND (u.plan_expires_at IS NULL OR u.plan_expires_at > ?) "
                "AND a.platform IN ('lichess', 'chesscom')", (time.time(),)
            )]

    # ---- usage & upgrades ---------------------------------------------------
    # ---- coach: students and homework ------------------------------------------------
    def add_student(self, coach_id: int, account_id: int, name: str) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO students (coach_id, account_id, name, created_at) VALUES (?, ?, ?, ?)",
                             (coach_id, account_id, name, time.time())).lastrowid

    def students(self, coach_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT s.*, u.email AS student_email FROM students s LEFT JOIN users u ON u.id = s.student_user_id "
                "WHERE s.coach_id = ? ORDER BY s.name COLLATE NOCASE", (coach_id,))]

    def student(self, student_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT s.*, u.email AS student_email FROM students s LEFT JOIN users u ON u.id = s.student_user_id "
                          "WHERE s.id = ?", (student_id,)).fetchone()
        return dict(r) if r else None

    def update_student(self, student_id: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("name", "note")}
        if allowed:
            with self._conn() as c:
                c.execute(f"UPDATE students SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), student_id))

    def delete_student(self, student_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM assignments WHERE student_id = ?", (student_id,))
            c.execute("DELETE FROM students WHERE id = ?", (student_id,))

    def create_invite(self, student_id: int, ttl_s: float = 14 * DAY) -> str:
        token = secrets.token_urlsafe(24)
        with self._conn() as c:
            c.execute("UPDATE students SET invite_hash = ?, invite_expires = ? WHERE id = ?", (_sha(token), time.time() + ttl_s, student_id))
        return token

    def accept_invite(self, token: str, user_id: int) -> dict | None:
        """Link a student's own login to a coach's roster entry. Single use."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM students WHERE invite_hash = ? AND invite_expires > ?", (_sha(token), time.time())).fetchone()
            if not r:
                return None
            c.execute("UPDATE students SET student_user_id = ?, joined_at = ?, invite_hash = NULL, invite_expires = NULL WHERE id = ?",
                      (user_id, time.time(), r["id"]))
        return self.student(r["id"])

    def coaches_of(self, user_id: int) -> list[dict]:
        """The roster entries a user has joined, with their coach."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT s.id, s.coach_id, s.name, s.joined_at, u.email AS coach_email, u.plan AS coach_plan, "
                "u.plan_expires_at AS coach_plan_expires_at FROM students s JOIN users u ON u.id = s.coach_id "
                "WHERE s.student_user_id = ?", (user_id,))]

    def leave_coach(self, user_id: int, student_id: int) -> bool:
        with self._conn() as c:
            n = c.execute("UPDATE students SET student_user_id = NULL, joined_at = NULL WHERE id = ? AND student_user_id = ?",
                          (student_id, user_id)).rowcount
        return n > 0

    def add_assignment(self, student_id: int, coach_id: int, title: str, detail: str, skill: str | None, due_at: float | None) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO assignments (student_id, coach_id, title, detail, skill, due_at, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", (student_id, coach_id, title, detail, skill, due_at, time.time())).lastrowid

    def assignments(self, student_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM assignments WHERE student_id = ? ORDER BY done_at IS NOT NULL, COALESCE(due_at, 9e18), id DESC", (student_id,))]

    def assignment(self, assignment_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
        return dict(r) if r else None

    def set_assignment_done(self, assignment_id: int, done: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE assignments SET done_at = ? WHERE id = ?", (time.time() if done else None, assignment_id))

    def delete_assignment(self, assignment_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))

    def usage_today(self, user_id: int, kind: str) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT count FROM usage WHERE user_id = ? AND day = ? AND kind = ?", (user_id, _today(), kind)
            ).fetchone()
        return r["count"] if r else 0

    def bump_usage(self, user_id: int, kind: str) -> int:
        with self._conn() as c:
            c.execute(
                "INSERT INTO usage (user_id, day, kind, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(user_id, day, kind) DO UPDATE SET count = count + 1",
                (user_id, _today(), kind),
            )
            return c.execute(
                "SELECT count FROM usage WHERE user_id = ? AND day = ? AND kind = ?", (user_id, _today(), kind)
            ).fetchone()["count"]

    def add_upgrade_request(self, user_id: int, note: str | None, plan: str = "pro") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO upgrade_requests (user_id, note, plan, created_at) VALUES (?, ?, ?, ?)",
                      (user_id, note, plan, time.time()))

    def upgrade_requests(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT r.id, r.user_id, r.note, r.plan AS requested, r.created_at, u.email, u.plan "
                "FROM upgrade_requests r JOIN users u ON u.id = r.user_id ORDER BY r.id DESC"
            )]


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
