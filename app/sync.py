"""Engine work as queue jobs: importing games, Prep Checks and instant previews; plus Pro auto-sync.

Each handler runs on a worker (see app/jobs.py). Imports are split into chunks so that:
  * the newest games are analysed first and the dashboard fills in after the first chunk;
  * between chunks the job goes to the back of the queue, so a 300-game import
    never blocks someone else's 20-game one.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import chess

from .analysis import analyse_game
from .config import PROJECT_ROOT, Settings
from .engine import Engine, EngineUnavailable
from .games import GameImportError, GameRecord, parse_pgn
from .jobs import JobError, JobQueue, Requeue, WorkerPool, drain
from .okf import Bundle, slug
from .plans import PLANS
from .player_bundle import build_player_bundle
from .prep import check_repertoire, parse_repertoire
from .profile import insights, rating_dna
from .sources import RateLimited, check_account, fetch_games
from .store import Store

log = logging.getLogger("plateau.sync")
DEMO_PGN = PROJECT_ROOT / "sample_data" / "demo_games.pgn"
DEMO_HANDLE = "demo_player"
SYNC_OVERLAP_S = 3600  # re-ask for the last hour on incremental syncs; duplicates are skipped by id
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MAX_RATE_RETRIES = 3
PLATFORM_LABELS = {"lichess": "Lichess", "chesscom": "Chess.com", "pgn": "ChessBase / PGN", "demo": "Demo"}


def player_key(account: dict) -> str:
    return f"u{account['user_id']}:{account['platform']}:{account['handle'].lower()}"


class Knowledge:
    """Owns each account's OKF bundle on disk: (re)builds it and serves cached, loaded copies.

    Bundles live next to the database (DB_PATH's folder)/bundles/<account>/, so they persist
    wherever the database does. The chess-principles bundle is mounted at /knowledge.
    """

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.root = Path(settings.db_path).resolve().parent / "bundles"
        self._cache: dict[Path, tuple[int, Bundle]] = {}
        self._lock = threading.Lock()

    def path(self, account: dict) -> Path:
        return self.root / slug(player_key(account), 120)

    def rebuild(self, account: dict, log_entry: str | None = None) -> Path | None:
        games = self.store.load_games(player_key(account))
        out = self.path(account)
        if not games or not self.store.account(account["id"]):
            shutil.rmtree(out, ignore_errors=True)
            return None
        reports = [r["report"] for r in self.store.repertoires(account["id"]) if r["report"]]
        with self._lock:
            out.parent.mkdir(parents=True, exist_ok=True)
            return build_player_bundle(out, games, account["handle"], account["platform"], reports, log_entry)

    def load(self, account: dict) -> Bundle | None:
        out = self.path(account)
        log_file = out / "log.md"
        if not log_file.exists() and self.rebuild(account) is None:
            return None
        stamp = log_file.stat().st_mtime_ns
        hit = self._cache.get(out)
        if hit and hit[0] == stamp:
            return hit[1]
        bundle = Bundle.load(out).mount(KNOWLEDGE_DIR, "/knowledge")
        self._cache[out] = (stamp, bundle)
        return bundle

    def remove(self, account: dict) -> None:
        shutil.rmtree(self.path(account), ignore_errors=True)
        self._cache.pop(self.path(account), None)


def run_prep_check(store: Store, account: dict, rep: dict, engine: Engine) -> dict:
    """Compare one uploaded repertoire with the account's games; store the report and its drills."""
    games = store.load_games(player_key(account))
    tree = parse_repertoire(rep["pgn"], chess.WHITE if rep["color"] == "white" else chess.BLACK)
    report = check_repertoire(tree, games, engine, rep["name"])
    report["repertoire_id"] = rep["id"]
    store.save_repertoire_report(rep["id"], report)
    store.add_prep_drills(player_key(account), rep["id"], report["drills"])
    return report


class Engines:
    """One worker's Stockfish processes, started on first use and reused across jobs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._main: Engine | None = None
        self._fast: Engine | None = None

    @property
    def main(self) -> Engine:
        if self._main is None:
            self._main = self._start(self.settings.engine_depth, self.settings.engine_time)
        return self._main

    @property
    def fast(self) -> Engine:
        """Shallower search for instant previews: a rough picture in seconds, not minutes."""
        if self._fast is None:
            self._fast = self._start(self.settings.preview_depth, self.settings.preview_time)
        return self._fast

    def _start(self, depth: int, t: float) -> Engine:
        try:
            return Engine(self.settings.stockfish_path, depth, t)
        except EngineUnavailable as exc:
            raise JobError(str(exc)) from exc

    def close(self) -> None:
        for e in (self._main, self._fast):
            if e is not None:
                e.close()
        self._main = self._fast = None


# ---- serialising pending games between chunks ------------------------------------------------
def _record_to_dict(r: GameRecord) -> dict:
    return dataclasses.asdict(r)


def _record_from_dict(d: dict) -> GameRecord:
    return GameRecord(**d)


class Importer:
    """The job handlers. Holds the store, settings and knowledge bundles; engines come from the worker."""

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.knowledge = Knowledge(store, settings)

    def handlers(self) -> dict[str, Callable]:
        return {"import": self.handle_import, "autosync": self.handle_import, "prep": self.handle_prep,
                "preview": self.handle_preview}

    # ---- import ----------------------------------------------------------------------------
    def _fetch(self, account: dict, p: dict) -> list[GameRecord]:
        platform = account["platform"]
        if platform in ("lichess", "chesscom"):
            since_ms = None
            if account.get("last_synced_at") and not p.get("full"):
                since_ms = int((account["last_synced_at"] - SYNC_OVERLAP_S) * 1000)
            return fetch_games(platform, account["handle"], p["max_games"], p.get("time_classes"), since_ms)
        if platform == "demo":
            return parse_pgn(Path(DEMO_PGN).read_text(), DEMO_HANDLE)
        if platform == "pgn":
            if not p.get("pgn"):
                raise GameImportError("Paste a PGN to import.")
            return parse_pgn(p["pgn"], account["handle"])
        raise GameImportError(f"Unknown platform '{platform}'.")

    def handle_import(self, job: dict, ctx) -> dict:
        account = self.store.account(job["account_id"])
        if not account:
            raise JobError("This account was removed.")
        key = player_key(account)
        p = dict(job["payload"])
        try:
            if "pending" not in p:  # first run: download and pick the new games
                ctx.progress(stage="Fetching games")
                try:
                    records = self._fetch(account, p)
                except RateLimited as exc:
                    if job["attempts"] >= MAX_RATE_RETRIES:
                        raise
                    raise Requeue(delay=65, stage=f"{exc} Retrying automatically in a minute.") from exc
                fetched_at = time.time()
                known = self.store.known_game_ids(key)
                # Newest first, so a big ChessBase database contributes its recent games, not its oldest.
                records.sort(key=lambda r: r.played_at.replace("?", "0"), reverse=True)
                new = [r for r in records if r.game_id not in known][: p["max_games"]]
                p = {"max_games": p["max_games"], "pending": [_record_to_dict(r) for r in new], "skipped": len(records) - len(new),
                     "total": len(new), "done": 0, "added": 0, "fetched_at": fetched_at}
                ctx.progress(stage="Waiting for the engine" if new else "Up to date", total=len(new), done=0, skipped=p["skipped"])
                if not new:
                    if not (self.knowledge.path(account) / "log.md").exists():
                        self.knowledge.rebuild(account)
                    self.store.update_account(account["id"], last_synced_at=fetched_at, last_sync_error=None)
                    return {"new_games": 0, "skipped": p["skipped"], "puzzles_added": 0}

            # One chunk of analysis.
            chunk = [_record_from_dict(d) for d in p["pending"][: self.settings.import_chunk]]
            rest = p["pending"][self.settings.import_chunk:]
            engine = ctx.resources["engines"].main
            ctx.progress(stage="Analysing with Stockfish", total=p["total"], done=p["done"], skipped=p["skipped"])
            analysed = []
            for g in chunk:
                analysed.append(analyse_game(g, engine))
                ctx.progress(done=p["done"] + len(analysed))
            if not self.store.account(account["id"]):  # removed (or the user deleted their account) while we worked
                raise JobError("This account was removed.")
            self.store.save_games(key, analysed)
            p["added"] += self.store.add_puzzles_from(key, analysed)
            if not self.store.account(account["id"]):  # removed between the check and the save: sweep what we wrote
                self.store.delete_player(key)
                self.knowledge.remove(account)
                raise JobError("This account was removed.")
            p["done"] += len(analysed)
            first_chunk = p["done"] == len(analysed)

            if rest:
                if first_chunk:  # show results while the rest imports
                    ctx.progress(stage="Writing your first results")
                    self.knowledge.rebuild(account)
                    ctx.partial(first_results=True, new_games=p["done"], puzzles_added=p["added"])
                p["pending"] = rest
                raise Requeue(payload=p, stage=f"Analysed {p['done']} of {p['total']}; continuing shortly")

            reps = self.store.repertoires(account["id"])
            if reps:
                ctx.progress(stage="Re-checking your repertoire")
                for rep in reps:
                    run_prep_check(self.store, account, rep, engine)
            ctx.progress(stage="Writing your knowledge bundle")
            self.knowledge.rebuild(account, f"**Update**: Added {p['done']} newly analysed games.")
            # The next incremental sync starts from when these games were *fetched*, not when analysis ended:
            # a long import can take over an hour, and games played meanwhile must not be skipped.
            self.store.update_account(account["id"], last_synced_at=p.get("fetched_at") or time.time(), last_sync_error=None)
            return {"new_games": p["done"], "skipped": p["skipped"], "puzzles_added": p["added"], "first_results": True}
        except GameImportError as exc:
            self.store.update_account(account["id"], last_sync_error=str(exc))
            raise JobError(str(exc)) from exc

    # ---- Prep Check ---------------------------------------------------------------------------
    def handle_prep(self, job: dict, ctx) -> dict:
        rep = self.store.repertoire(job["payload"]["repertoire_id"])
        account = self.store.account(job["account_id"])
        if not rep or not account:
            raise JobError("That repertoire or account was removed.")
        ctx.progress(stage="Comparing your repertoire with your games")
        try:
            report = run_prep_check(self.store, account, rep, ctx.resources["engines"].main)
        except GameImportError as exc:
            raise JobError(str(exc)) from exc
        ctx.progress(stage="Updating your knowledge bundle")
        self.knowledge.rebuild(account, f"**Update**: Checked repertoire '{rep['name']}' ({rep['color']}).")
        return {"repertoire_id": rep["id"], "drills": len(report["drills"])}

    # ---- instant preview (no account needed) ---------------------------------------------------
    def handle_preview(self, job: dict, ctx) -> dict:
        p = job["payload"]
        n = self.settings.preview_games
        ctx.progress(stage="Finding your games")
        try:
            handle = check_account(p["platform"], p["username"])
            try:
                records = fetch_games(p["platform"], handle, n, ["blitz", "rapid", "classical"])
            except GameImportError:
                records = fetch_games(p["platform"], handle, n, ["bullet", "blitz", "rapid", "classical"])
        except RateLimited as exc:
            if job["attempts"] >= MAX_RATE_RETRIES:
                raise JobError(str(exc)) from exc
            raise Requeue(delay=65, stage="Lichess is busy; trying again in a minute.") from exc
        except GameImportError as exc:
            raise JobError(str(exc)) from exc
        records = sorted(records, key=lambda r: r.played_at, reverse=True)[:n]
        engine = ctx.resources["engines"].fast
        ctx.progress(stage="Analysing your recent games", total=len(records), done=0)
        games = []
        for i, r in enumerate(records, 1):
            games.append(analyse_game(r, engine))
            ctx.progress(done=i)
        return preview_summary(handle, p["platform"], games)


def preview_summary(handle: str, platform: str, games: list) -> dict:
    """The public teaser: Rating DNA, the top findings and the single costliest moment."""
    dna = rating_dna(games)
    ins = insights(games, dna)[:3]
    worst = None
    for g in games:
        for m in g.moves:
            if m.classification in ("mistake", "blunder") and m.best_san and (worst is None or m.win_loss > worst[1].win_loss):
                worst = (g, m)
    moment = None
    if worst:
        g, m = worst
        moment = {"fen": m.fen_before, "you_played": m.played_san, "engine_best": m.best_san, "engine_line": m.best_line[:5],
                  "move": f"{m.move_number}{'.' if m.color == 'white' else '...'}{m.played_san}", "opponent": g.opponent,
                  "date": g.played_at, "winning_chances_lost": round(m.win_loss), "phase": m.phase, "game_url": g.url,
                  "played_uci": m.played, "best_uci": m.best}
    return {"handle": handle, "platform": platform, "games": len(games), "dna": dna, "insights": ins, "moment": moment}


class Jobs:
    """The app's handle on the queue: submit work, see progress, start/stop workers."""

    def __init__(self, queue: JobQueue, importer: Importer, settings: Settings):
        self.queue, self.importer, self.settings = queue, importer, settings
        self.mode = settings.job_mode
        self._inline_resources: dict | None = None
        self.pool: WorkerPool | None = None

    def resources(self) -> dict:
        return {"engines": Engines(self.settings)}

    def submit(self, kind: str, payload: dict, user_id: int | None = None, account_id: int | None = None,
               priority: int | None = None, exclusive: bool = False) -> str | None:
        """Queue a job. exclusive=True: refuse (return None) if the account already has one queued or running."""
        if exclusive and account_id is not None:
            job_id = self.queue.enqueue_for_account(kind, payload, user_id, account_id, priority)
            if job_id is None:
                return None
        else:
            job_id = self.queue.enqueue(kind, payload, user_id, account_id, priority)
        if self.mode == "inline":
            if self._inline_resources is None:
                self._inline_resources = self.resources()
            drain(self.queue, self.importer.handlers(), self._inline_resources, job_id)
        return job_id

    def start(self, name: str = "web", orphan_check: bool = True) -> None:
        """Start worker threads (thread mode in the web process, or always in the worker process)."""
        self.pool = WorkerPool(self.queue, self.importer.handlers(), self.resources,
                               workers=self.settings.max_concurrent_imports, name=name)
        if orphan_check:
            self.queue.recover(stale_after=0)  # single process: anything 'running' at startup is an orphan
        self.pool.start()

    def stop(self) -> None:
        if self.pool:
            self.pool.stop()
        if self._inline_resources:
            for r in self._inline_resources.values():
                r.close()

    def workers_alive(self) -> int | None:
        """None when workers run in another process (check the queue heartbeat instead)."""
        if self.pool:
            return self.pool.alive()
        return 0 if self.mode == "thread" else None


class AutoSync(threading.Thread):
    """Background thread: queues a low-priority sync for Pro users' accounts that have auto-sync on."""

    def __init__(self, jobs: Jobs, store: Store, settings: Settings, interval_minutes: float):
        super().__init__(daemon=True, name="autosync")
        self.jobs, self.store, self.settings = jobs, store, settings
        self.interval = max(5.0, interval_minutes) * 60
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sync_all()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("auto-sync round failed")

    def sync_all(self) -> int:
        queued = 0
        for acct in self.store.auto_sync_accounts():
            if self._stop.is_set():
                break
            if acct["platform"] == "chesscom" and not self.settings.chesscom_enabled:
                continue
            classes = [c for c in (acct.get("time_classes") or "blitz,rapid,classical").split(",") if c]
            cap = min(PLANS["pro"]["max_games_per_sync"], self.settings.max_games)
            if self.jobs.submit("autosync", {"max_games": cap, "time_classes": classes}, acct["user_id"], acct["id"], exclusive=True):
                queued += 1
        return queued

    def stop(self) -> None:
        self._stop.set()
