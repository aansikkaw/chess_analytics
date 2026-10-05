"""SQLite persistence: analysed games and the puzzle deck (with spaced repetition).

One connection per call keeps this safe to use from FastAPI's worker threads.
"""

from __future__ import annotations

import json
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
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
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
        with self._conn() as c:
            rows = c.execute("SELECT data FROM games WHERE username = ?", (username.lower(),)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["moves"] = [MoveAnalysis(**m) for m in d["moves"]]
            out.append(GameAnalysis(**d))
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

    @staticmethod
    def _puzzle_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["line"] = json.loads(d["line"])
        d["themes"] = json.loads(d["themes"])
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
