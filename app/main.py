"""HTTP API + static front end.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import chess
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analysis import analyse_games, win_percent
from .coach import Coach, CoachTools
from .config import PROJECT_ROOT, load_settings
from .engine import Engine, EngineUnavailable
from .games import GameImportError, fetch_lichess_pgn, parse_pgn, validate_username
from .profile import insights, rating_dna, weekly_plan
from .store import Store

STATIC = Path(__file__).parent / "static"
DEMO_PGN = PROJECT_ROOT / "sample_data" / "demo_games.pgn"
DEMO_USER = "demo_player"
MAX_JOBS_KEPT = 100
ALSO_GOOD_WIN_PCT = 3.0  # a puzzle answer within 3 win-% points of the best move also counts

settings = load_settings()
store = Store(settings.db_path)
state: dict = {"engine": None, "engine_error": None}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # One long-lived engine for interactive requests (puzzle checks, coach).
    try:
        state["engine"] = Engine(settings.stockfish_path, settings.engine_depth, settings.engine_time)
    except EngineUnavailable as exc:
        state["engine_error"] = str(exc)
    yield
    if state["engine"]:
        state["engine"].close()


app = FastAPI(title="Plateau Breaker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---- models -----------------------------------------------------------------
class ImportRequest(BaseModel):
    username: str
    source: str = Field("lichess", pattern="^(lichess|pgn|demo)$")
    pgn: str | None = Field(None, max_length=5_000_000)
    max_games: int = Field(40, ge=1, le=200)


class AttemptRequest(BaseModel):
    move: str = Field(..., max_length=10)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=40)


# ---- import jobs ------------------------------------------------------------
def _set_job(job_id: str, **fields) -> None:
    with jobs_lock:
        jobs[job_id].update(fields)


def _run_import(job_id: str, req: ImportRequest) -> None:
    """Runs in a worker thread. Uses its own engine so it never blocks the UI engine."""
    try:
        username = DEMO_USER if req.source == "demo" else validate_username(req.username)
        _set_job(job_id, stage="Fetching games")
        if req.source == "lichess":
            pgn = fetch_lichess_pgn(username, min(req.max_games, settings.max_games))
        elif req.source == "pgn":
            pgn = req.pgn or ""
        else:
            pgn = DEMO_PGN.read_text()
        games = parse_pgn(pgn, username)[: settings.max_games]
        known = store.known_game_ids(username)
        new = [g for g in games if g.game_id not in known]
        _set_job(job_id, stage="Analysing with Stockfish", total=len(new), done=0, skipped=len(games) - len(new))
        if new:
            with Engine(settings.stockfish_path, settings.engine_depth, settings.engine_time) as engine:
                analysed = analyse_games(new, engine, lambda i, n: _set_job(job_id, done=i))
            store.save_games(username, analysed)
            added = store.add_puzzles_from(username, analysed)
        else:
            added = 0
        _set_job(job_id, state="done", stage="Done", username=username, puzzles_added=added)
    except (GameImportError, EngineUnavailable) as exc:
        _set_job(job_id, state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        _set_job(job_id, state="error", error=f"Unexpected error: {exc}")


@app.post("/api/import")
def start_import(req: ImportRequest, background: BackgroundTasks) -> dict:
    if req.source == "pgn" and not (req.pgn or "").strip():
        raise HTTPException(400, "Paste a PGN to import.")
    if req.source != "demo":
        try:
            validate_username(req.username)
        except GameImportError as exc:
            raise HTTPException(400, str(exc)) from exc
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        finished = [k for k, j in jobs.items() if j["state"] != "running"]
        for k in finished[: max(0, len(jobs) - MAX_JOBS_KEPT)]:
            del jobs[k]
        jobs[job_id] = {"id": job_id, "state": "running", "stage": "Queued", "done": 0, "total": 0}
    background.add_task(_run_import, job_id, req)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "No such job.")
        return dict(job)


# ---- player views -----------------------------------------------------------
def _games_or_404(username: str):
    games = store.load_games(username)
    if not games:
        raise HTTPException(404, f"No analysed games for '{username}'. Import some first.")
    return games


@app.get("/api/players/{username}/profile")
def profile(username: str, rating: int | None = None) -> dict:
    games = _games_or_404(username)
    dna = rating_dna(games, rating)
    recent = sorted(games, key=lambda g: g.played_at, reverse=True)[:10]
    return {
        "username": username,
        "dna": dna,
        "insights": insights(games, dna),
        "due_puzzles": store.count_due(username),
        "recent_games": [
            {"game_id": g.game_id, "opponent": g.opponent, "result": g.result, "score": g.score,
             "color": g.player_color, "accuracy": g.accuracy, "opening": g.opening, "played_at": g.played_at}
            for g in recent
        ],
    }


@app.get("/api/players/{username}/plan")
def plan(username: str, minutes: int = 45, rating: int | None = None) -> dict:
    games = _games_or_404(username)
    return weekly_plan(rating_dna(games, rating), store.count_due(username), max(15, min(minutes, 180)))


@app.get("/api/players/{username}/puzzles")
def puzzles(username: str, due: bool = True, limit: int = 20) -> list[dict]:
    rows = store.puzzles(username, due_only=due, limit=max(1, min(limit, 100)))
    for r in rows:  # don't ship the answer to the browser before the attempt
        for k in ("solution", "solution_san", "line"):
            r.pop(k, None)
    return rows


@app.post("/api/puzzles/{puzzle_id}/attempt")
def attempt(puzzle_id: int, req: AttemptRequest) -> dict:
    p = store.get_puzzle(puzzle_id)
    if not p:
        raise HTTPException(404, "No such puzzle.")
    board = chess.Board(p["fen"])
    try:
        move = chess.Move.from_uci(req.move)
    except ValueError:
        try:
            move = board.parse_san(req.move)
        except ValueError as exc:
            raise HTTPException(400, f"Couldn't read the move '{req.move}'.") from exc
    # Auto-promote to a queen if the UI sent a pawn move to the last rank without a piece.
    if move not in board.legal_moves and move.promotion is None:
        promo = chess.Move(move.from_square, move.to_square, chess.QUEEN)
        move = promo if promo in board.legal_moves else move
    if move not in board.legal_moves:
        raise HTTPException(400, "That move isn't legal here.")

    verdict, note = "wrong", None
    if move.uci() == p["solution"]:
        verdict = "correct"
    elif state["engine"] is not None:
        side = board.turn
        best = state["engine"].evaluate(board)
        after_board = board.copy()
        after_board.push(move)
        after = state["engine"].evaluate(after_board)
        gap = win_percent(best.cp_for(side)) - win_percent(after.cp_for(side))
        if gap <= ALSO_GOOD_WIN_PCT:
            verdict, note = "also_good", "Not the engine's first choice, but just as good."
    srs = store.record_attempt(puzzle_id, verdict != "wrong")
    return {
        "verdict": verdict,
        "note": note,
        "your_move": board.san(move),
        "solution": p["solution"],
        "solution_san": p["solution_san"],
        "line": p["line"],
        "you_played_in_game": p["played_san"],
        **srs,
    }


@app.post("/api/players/{username}/coach")
def coach(username: str, req: ChatRequest) -> dict:
    games = _games_or_404(username)
    tools = CoachTools(games, state["engine"], store.count_due(username))
    bot = Coach(username, tools, settings.anthropic_api_key, settings.coach_model)
    try:
        r = bot.chat(req.message, req.history)
    except Exception as exc:  # noqa: BLE001 - API/network errors shouldn't 500 the chat
        raise HTTPException(502, f"The coach couldn't answer: {exc}") from exc
    return {"reply": r.reply, "mode": r.mode, "tools_used": r.tools_used}


@app.get("/api/health")
def health() -> dict:
    return {
        "engine": state["engine"] is not None,
        "engine_error": state["engine_error"],
        "coach_mode": "agent" if settings.anthropic_api_key else "offline",
        "demo_available": DEMO_PGN.exists(),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
