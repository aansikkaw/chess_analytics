"""Opponent Library: a ChessBase-style database of the players you prepare against.

Every player you scout (or upload a PGN for) is saved. Their games are stored once on the server and
shared by everyone who scouts the same public Lichess or Chess.com account, so a second visit opens
instantly and "Update" downloads only games played since the last time. Uploaded PGNs (over-the-board
opponents) stay private to the person who uploaded them.

Every position in every stored game (up to move 40) is indexed by its Zobrist hash, the same 64-bit
position key opening books use. That gives ChessBase-style position search: set up any position and see
every game in your library that reached it, by any move order, with what was played next and how it scored.

Tables (created in store.SCHEMA):
  lib_players    one row per player: platform, handle, owner (0 = shared public account), game count
  lib_games      their games (the parsed GameRecord as JSON, plus columns for filtering)
  lib_positions  (position hash, game, ply, next move) for position search
  library        which players each user has saved, with their notes and tags
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
from collections import defaultdict
from collections.abc import Iterable

import chess
import chess.pgn
import chess.polyglot

from .games import GameRecord

INDEX_PLIES = 80  # positions indexed per game: the first 40 moves, where games share positions
ALL_CLASSES = ["bullet", "blitz", "rapid", "classical"]
OVERLAP_S = 86_400  # incremental updates re-read the last day, so nothing is missed between fetches
FRESH_S = 30 * 60  # a player updated this recently (by anyone) isn't downloaded again
PLATFORMS = {"lichess": "Lichess", "chesscom": "Chess.com", "pgn": "PGN upload"}


def position_key(board: chess.Board) -> int:
    """The position's Zobrist hash as a signed 64-bit integer (what SQLite stores)."""
    h = chess.polyglot.zobrist_hash(board)
    return h - (1 << 64) if h >= (1 << 63) else h


def record_to_json(rec: GameRecord) -> str:
    return json.dumps(dataclasses.asdict(rec), separators=(",", ":"))


def record_from_json(text: str) -> GameRecord:
    d = json.loads(text)
    d["player_color"] = bool(d["player_color"])
    return GameRecord(**d)


def _color(rec: GameRecord) -> str:
    return "white" if rec.player_color == chess.WHITE else "black"


class Library:
    def __init__(self, store):
        self.store = store

    def _conn(self):
        return self.store._conn()

    # ---- players --------------------------------------------------------------------------------
    def get_or_create_player(self, platform: str, handle: str, owner_id: int = 0) -> dict:
        key = handle.strip().lower()
        with self._conn() as c:
            row = c.execute("SELECT * FROM lib_players WHERE platform = ? AND handle_key = ? AND owner_id = ?",
                            (platform, key, owner_id)).fetchone()
            if row:
                return dict(row)
            pid = c.execute("INSERT INTO lib_players (platform, handle, handle_key, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                            (platform, handle.strip(), key, owner_id, time.time())).lastrowid
            return dict(c.execute("SELECT * FROM lib_players WHERE id = ?", (pid,)).fetchone())

    def player(self, player_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM lib_players WHERE id = ?", (player_id,)).fetchone()
        return dict(row) if row else None

    def needs_download(self, player: dict, want: int) -> bool:
        """True when a search should hit the network: never fetched, asked for more games than we hold, or stale."""
        if not player.get("fetched_at"):
            return True
        if want > (player.get("requested") or 0) and player["games"] < want:
            return True
        return time.time() - player["fetched_at"] > FRESH_S

    def fetch_plan(self, player: dict, want: int) -> int | None:
        """since_ms for an incremental update, or None for a full download (first time, or more games wanted)."""
        if not player.get("fetched_at") or (want > (player.get("requested") or 0) and player["games"] < want):
            return None
        return int((player["fetched_at"] - OVERLAP_S) * 1000)

    def add_games(self, player_id: int, records: Iterable[GameRecord], requested: int | None = None) -> int:
        """Store new games (duplicates are skipped) and index their positions. Returns how many were new."""
        added = 0
        with self._conn() as c:
            for rec in records:
                cur = c.execute(
                    "INSERT OR IGNORE INTO lib_games (player_id, game_id, color, opponent, result, score, time_class, opening, "
                    "played_at, plies, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (player_id, rec.game_id, _color(rec), rec.black if rec.player_color == chess.WHITE else rec.white,
                     rec.result, rec.player_score, rec.time_class or "", rec.opening or "", rec.played_at or "", len(rec.moves),
                     record_to_json(rec)))
                if not cur.rowcount:
                    continue
                added += 1
                ref = cur.lastrowid
                board, rows = chess.Board(), []
                for ply, uci in enumerate(rec.moves[:INDEX_PLIES]):
                    rows.append((position_key(board), ref, ply, uci))
                    try:
                        board.push(chess.Move.from_uci(uci))
                    except ValueError:
                        break
                else:
                    if len(rec.moves) <= INDEX_PLIES:  # the final position too, so endings of short games are found
                        rows.append((position_key(board), ref, len(rec.moves), None))
                c.executemany("INSERT INTO lib_positions (hash, game_ref, ply, next_uci) VALUES (?, ?, ?, ?)", rows)
            n = c.execute("SELECT COUNT(*) FROM lib_games WHERE player_id = ?", (player_id,)).fetchone()[0]
            if requested is not None:
                c.execute("UPDATE lib_players SET games = ?, fetched_at = ?, requested = MAX(COALESCE(requested, 0), ?) WHERE id = ?",
                          (n, time.time(), requested, player_id))
            else:
                c.execute("UPDATE lib_players SET games = ?, fetched_at = ? WHERE id = ?", (n, time.time(), player_id))
        return added

    def records(self, player_id: int, limit: int | None = None) -> list[GameRecord]:
        sql = "SELECT data FROM lib_games WHERE player_id = ? ORDER BY played_at DESC, id DESC"
        args: list = [player_id]
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._conn() as c:
            return [record_from_json(r["data"]) for r in c.execute(sql, args)]

    # ---- a user's saved players ---------------------------------------------------------------------
    def save(self, user_id: int, player_id: int) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO library (user_id, player_id, added_at) VALUES (?, ?, ?)", (user_id, player_id, time.time()))
            c.execute("UPDATE library SET opened_at = ? WHERE user_id = ? AND player_id = ?", (time.time(), user_id, player_id))

    def count(self, user_id: int) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM library WHERE user_id = ?", (user_id,)).fetchone()[0]

    def has(self, user_id: int, player_id: int) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM library WHERE user_id = ? AND player_id = ?", (user_id, player_id)).fetchone() is not None

    def entries(self, user_id: int) -> list[dict]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT p.id, p.platform, p.handle, p.owner_id, p.games, p.fetched_at, l.notes, l.tags, l.added_at, l.opened_at, "
                "(SELECT MAX(played_at) FROM lib_games g WHERE g.player_id = p.id) AS last_game, "
                "(SELECT ROUND(100.0 * AVG(score)) FROM lib_games g WHERE g.player_id = p.id) AS score_pct "
                "FROM library l JOIN lib_players p ON p.id = l.player_id WHERE l.user_id = ? "
                "ORDER BY COALESCE(l.opened_at, l.added_at) DESC", (user_id,))]
        for r in rows:
            r["tags"] = [t for t in (r["tags"] or "").split(",") if t]
            r["platform_name"] = PLATFORMS.get(r["platform"], r["platform"])
        return rows

    def entry(self, user_id: int, player_id: int) -> dict | None:
        return next((e for e in self.entries(user_id) if e["id"] == player_id), None)

    def update_entry(self, user_id: int, player_id: int, notes: str | None = None, tags: list[str] | None = None) -> None:
        with self._conn() as c:
            if notes is not None:
                c.execute("UPDATE library SET notes = ? WHERE user_id = ? AND player_id = ?", (notes, user_id, player_id))
            if tags is not None:
                clean = sorted({t.strip().lower()[:24] for t in tags if t.strip()})[:12]
                c.execute("UPDATE library SET tags = ? WHERE user_id = ? AND player_id = ?", (",".join(clean), user_id, player_id))

    def remove(self, user_id: int, player_id: int) -> bool:
        """Take a player out of your library. A private PGN player (only you can see it) is deleted with its games."""
        with self._conn() as c:
            gone = c.execute("DELETE FROM library WHERE user_id = ? AND player_id = ?", (user_id, player_id)).rowcount
            p = c.execute("SELECT owner_id FROM lib_players WHERE id = ?", (player_id,)).fetchone()
            if gone and p and p["owner_id"] == user_id:
                self._delete_player(c, player_id)
        return bool(gone)

    @staticmethod
    def _delete_player(c, player_id: int) -> None:
        c.execute("DELETE FROM lib_positions WHERE game_ref IN (SELECT id FROM lib_games WHERE player_id = ?)", (player_id,))
        c.execute("DELETE FROM lib_games WHERE player_id = ?", (player_id,))
        c.execute("DELETE FROM twins WHERE player_id = ?", (player_id,))
        c.execute("DELETE FROM lib_players WHERE id = ?", (player_id,))

    def delete_user(self, user_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM library WHERE user_id = ?", (user_id,))
            for r in c.execute("SELECT id FROM lib_players WHERE owner_id = ?", (user_id,)).fetchall():
                self._delete_player(c, r["id"])

    def owned_ids(self, user_id: int, wanted: Iterable[int] | None) -> list[int]:
        """The player ids from `wanted` that are in this user's library (all of them if `wanted` is empty)."""
        with self._conn() as c:
            mine = [r[0] for r in c.execute("SELECT player_id FROM library WHERE user_id = ?", (user_id,))]
        if not wanted:
            return mine
        allowed = set(mine)
        return [i for i in dict.fromkeys(wanted) if i in allowed]

    # ---- search ----------------------------------------------------------------------------------------
    @staticmethod
    def _where(player_ids: list[int], f: dict) -> tuple[str, list]:
        sql = [f"g.player_id IN ({','.join('?' * len(player_ids))})"]
        args: list = list(player_ids)
        if f.get("color") in ("white", "black"):
            sql.append("g.color = ?")
            args.append(f["color"])
        if f.get("result") in ("win", "draw", "loss"):
            sql.append("g.score = ?")
            args.append({"win": 1.0, "draw": 0.5, "loss": 0.0}[f["result"]])
        classes = [t for t in (f.get("time_classes") or []) if t in ALL_CLASSES]
        if classes:
            sql.append(f"g.time_class IN ({','.join('?' * len(classes))})")
            args += classes
        for col, key in (("g.opening", "opening"), ("g.opponent", "opponent")):
            if f.get(key):
                sql.append(f"{col} LIKE ? ESCAPE '\\'")  # column names are constants above
                args.append("%" + f[key].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if f.get("date_from"):
            sql.append("g.played_at >= ?")
            args.append(f["date_from"].replace("-", "."))
        if f.get("date_to"):
            sql.append("g.played_at <= ?")
            args.append(f["date_to"].replace("-", ".") + "~")  # "~" sorts after any time part
        return " AND ".join(sql), args

    def games(self, player_ids: list[int], filters: dict, limit: int = 50, offset: int = 0) -> dict:
        if not player_ids:
            return {"total": 0, "games": [], "summary": None}
        where, args = self._where(player_ids, filters)
        with self._conn() as c:
            total, score = c.execute(f"SELECT COUNT(*), AVG(g.score) FROM lib_games g WHERE {where}", args).fetchone()
            rows = [dict(r) for r in c.execute(
                f"SELECT g.id, g.player_id, p.handle AS player, g.game_id, g.color, g.opponent, g.result, g.score, g.time_class, "
                f"g.opening, g.played_at, g.plies FROM lib_games g JOIN lib_players p ON p.id = g.player_id WHERE {where} "
                "ORDER BY g.played_at DESC, g.id DESC LIMIT ? OFFSET ?", [*args, limit, offset])]
        for r in rows:
            r["moves"] = (r.pop("plies") + 1) // 2
        return {"total": total, "games": rows, "score_pct": round(100 * score) if score is not None else None}

    def explore(self, player_ids: list[int], board: chess.Board, filters: dict, list_games: int = 25) -> dict:
        """Every stored game (of these players, after filters) that reached this position, by any move order:
        what was played next, how each move scored, and the games themselves."""
        out = {"fen": board.fen(), "turn": "white" if board.turn == chess.WHITE else "black", "games": 0, "moves": [], "list": []}
        if not player_ids:
            return out
        where, args = self._where(player_ids, filters)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT pos.next_uci, pos.ply, g.id, g.game_id, g.result, g.score, g.color, g.opponent, g.played_at, g.time_class, "
                f"g.opening, p.handle AS player FROM lib_positions pos JOIN lib_games g ON g.id = pos.game_ref "
                f"JOIN lib_players p ON p.id = g.player_id WHERE pos.hash = ? AND {where} ORDER BY g.played_at DESC",
                [position_key(board), *args]).fetchall()
        seen_game: set[str] = set()
        by_move: dict[str | None, dict] = defaultdict(lambda: {"games": set(), "w": 0, "d": 0, "b": 0, "their": []})
        listed: list[dict] = []
        for r in rows:
            m = by_move[r["next_uci"]]
            m["their"].append(r["score"])
            if r["game_id"] not in m["games"]:  # a game between two library players counts once
                m["games"].add(r["game_id"])
                m["w" if r["result"] == "1-0" else "b" if r["result"] == "0-1" else "d"] += 1
            if r["game_id"] not in seen_game:
                seen_game.add(r["game_id"])
                if len(listed) < list_games:
                    listed.append({k: r[k] for k in ("id", "game_id", "result", "score", "color", "opponent", "played_at",
                                                     "time_class", "opening", "player")} | {"move": r["ply"] // 2 + 1})
        moves = []
        for uci, m in by_move.items():
            if uci is None:
                continue
            try:
                mv = chess.Move.from_uci(uci)
                san = board.san(mv) if mv in board.legal_moves else None
            except ValueError:
                san = None
            if not san:
                continue
            n = len(m["games"])
            moves.append({"uci": uci, "san": san, "games": n, "white_pct": round(100 * m["w"] / n), "draw_pct": round(100 * m["d"] / n),
                          "black_pct": round(100 * m["b"] / n), "their_score_pct": round(100 * sum(m["their"]) / len(m["their"]))})
        moves.sort(key=lambda x: (-x["games"], x["san"]))
        out.update(games=len(seen_game), moves=moves, list=listed)
        return out

    def game(self, game_ref: int) -> tuple[dict, GameRecord] | None:
        with self._conn() as c:
            row = c.execute("SELECT g.*, p.handle AS player, p.platform FROM lib_games g JOIN lib_players p ON p.id = g.player_id "
                            "WHERE g.id = ?", (game_ref,)).fetchone()
        if not row:
            return None
        return dict(row), record_from_json(row["data"])

    def pgn(self, player_ids: list[int], filters: dict, limit: int = 5000) -> str:
        """The selected games as one PGN file, ready for ChessBase."""
        if not player_ids:
            return ""
        where, args = self._where(player_ids, filters)
        with self._conn() as c:
            rows = c.execute(f"SELECT g.data FROM lib_games g WHERE {where} ORDER BY g.played_at DESC LIMIT ?", [*args, limit]).fetchall()
        seen: set[str] = set()
        out = io.StringIO()
        for r in rows:
            rec = record_from_json(r["data"])
            if rec.game_id in seen:
                continue
            seen.add(rec.game_id)
            print(to_pgn(rec), file=out, end="\n\n")
        return out.getvalue()


def to_pgn(rec: GameRecord) -> str:
    game = chess.pgn.Game()
    h = game.headers
    h["Event"] = f"Rated {rec.time_class} game" if rec.time_class else "?"
    h["Site"] = rec.url or "?"
    h["Date"] = rec.played_at or "????.??.??"
    h["White"], h["Black"], h["Result"] = rec.white, rec.black, rec.result
    if rec.player_rating:
        h["WhiteElo" if rec.player_color == chess.WHITE else "BlackElo"] = str(rec.player_rating)
    if rec.time_control and rec.time_control != "-":
        h["TimeControl"] = rec.time_control
    if rec.opening:
        h["Opening"] = rec.opening
    if rec.termination:
        h["Termination"] = rec.termination
    node = game
    for uci in rec.moves:
        try:
            node = node.add_variation(chess.Move.from_uci(uci))
        except ValueError:
            break
    return str(game)


def board_from(moves: list[str] | None = None, fen: str | None = None) -> chess.Board:
    """The position to explore: a FEN, or moves (UCI or SAN) from the start. Raises ValueError if invalid."""
    board = chess.Board(fen) if fen else chess.Board()
    if fen and not board.is_valid():
        raise ValueError("That FEN isn't a legal chess position.")
    for m in moves or []:
        try:
            mv = chess.Move.from_uci(m)
            if mv not in board.legal_moves:
                raise ValueError
        except ValueError:
            mv = board.parse_san(m)  # raises ValueError for anything illegal
        board.push(mv)
    return board
