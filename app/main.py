"""HTTP API + static front end.

Run:  uvicorn app.main:app --reload

Every player route checks that the linked chess account belongs to the signed-in
user, and every Pro route checks the plan. Data never crosses accounts.
Engine-heavy work (imports, Prep Checks, previews) goes through the job queue (app/jobs.py).
"""

from __future__ import annotations

import io
import logging
from html import escape as html_escape
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import chess
import chess.engine
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .analysis import win_percent
from .coach import Coach, CoachTools
from .config import load_settings
from .engine import Engine, EngineUnavailable
from .games import TIME_CLASSES, GameImportError, decode_upload, name_matches, player_names, validate_pgn_name, validate_username
from .jobs import JobQueue
from .llm import resolve_backends
from .mailer import MailError, Mailer, coach_invite_email, reset_email, support_email, verification_email
from .observability import analytics_config, capture_client_error, init_sentry, sentry_enabled
from .plans import (EVENT_PASS_DAYS, FEATURE_NAMES, OFFER_IDS, PLANS, admin_emails, effective_plan, has_feature, is_coach, is_pro, plan_of,
                    public_plans)
from .player_bundle import PRO_PREFIXES
from .prep import detect_color, parse_repertoire
from .profile import SKILLS, insights, rating_dna, weekly_plan
from .progress import progress_report
from .repertoire import repertoire_report
from .scout import scout_report
from .sources import check_account, fetch_games
from .status import CoachStats, StatusChecker
from .store import Store
from .sync import DEMO_HANDLE, DEMO_PGN, KNOWLEDGE_DIR, AutoSync, Importer, Jobs, player_key

log = logging.getLogger("plateau")
STATIC = Path(__file__).parent / "static"
ALSO_GOOD_WIN_PCT = 3.0
VERIFY_TTL_S = 48 * 3600
RESET_TTL_S = 3600
PREVIEW_CACHE_S = 3600

settings = load_settings()
init_sentry(settings, "web")
store = Store(settings.db_path)
importer = Importer(store, settings)
queue = JobQueue(settings.db_path)
jobs = Jobs(queue, importer, settings)
mailer = Mailer(settings, store)
coach_stats = CoachStats()
login_limiter = auth.LoginLimiter()
preview_limiter = auth.LoginLimiter(limit=settings.previews_per_hour, window=3600)
forgot_limiter = auth.LoginLimiter(limit=5, window=3600)
support_limiter = auth.LoginLimiter(limit=5, window=3600)
client_error_limiter = auth.LoginLimiter(limit=30, window=600)
resend_limiter = auth.LoginLimiter(limit=3, window=3600)
analyse_limiter = auth.LoginLimiter(limit=240, window=3600)  # engine look-ups from Explore, per user
signup_limiter = auth.LoginLimiter(limit=settings.signups_per_hour, window=3600)
test_email_limiter = auth.LoginLimiter(limit=10, window=3600)
DUMMY_HASH = auth.hash_password("timing-equaliser-not-a-real-password")
state: dict = {"engine": None, "engine_error": None, "autosync": None}
status_checker = StatusChecker(settings, store, jobs, mailer, coach_stats, state, lambda: resolve_backends(settings))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Reference-counted so that nested startups (tests, reloads) share one Stockfish process
    # instead of leaking one each: every Stockfish holds a ~100 MB neural network in memory.
    state["lifespans"] = state.get("lifespans", 0) + 1
    if state["lifespans"] == 1:
        try:
            state["engine"] = Engine(settings.stockfish_path, settings.engine_depth, settings.engine_time)
        except EngineUnavailable as exc:
            state["engine_error"] = str(exc)
        if settings.job_mode == "thread":
            jobs.start()
        if settings.autosync_enabled:
            state["autosync"] = AutoSync(jobs, store, settings, settings.autosync_interval_min)
            state["autosync"].start()
    try:
        yield
    finally:
        state["lifespans"] -= 1
        if state["lifespans"] == 0:
            if state["autosync"]:
                state["autosync"].stop()
                state["autosync"] = None
            jobs.stop()
            if state["engine"]:
                state["engine"].close()
                state["engine"] = None


app = FastAPI(title="Plateau Breaker", version=settings.app_version, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _csp() -> str:
    a = analytics_config(settings)
    script, connect = ["'self'"], ["'self'"]
    if a["provider"] == "plausible":
        host = "/".join(a["src"].split("/")[:3])
        script.append(host)
        connect.append(host)
    elif a["provider"] == "posthog":
        script += [a["host"], "https://*.posthog.com"]
        connect += [a["host"], "https://*.posthog.com"]
    return ("default-src 'self'; "
            f"script-src {' '.join(script)}; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data: blob:; "
            f"connect-src {' '.join(connect)}; "
            "manifest-src 'self'; worker-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


CSP = _csp()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    path = request.url.path
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif path.startswith(("/static/fonts/", "/static/icons/")):
        resp.headers["Cache-Control"] = "public, max-age=604800"
    elif path.startswith("/static/"):
        # JS/CSS URLs aren't versioned, so browsers must check for a new copy (a cheap 304) after every deploy.
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---- helpers ----------------------------------------------------------------------
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def current_user(request: Request) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    user = store.session_user(auth.token_hash(token)) if token else None
    if not user:
        raise HTTPException(401, "Please sign in.")
    return with_sponsor(user)


def with_sponsor(user: dict) -> dict:
    """A free user who has joined a coach's squad gets Pro for as long as the coach is on the Coach plan."""
    if effective_plan(user) != "free":
        return user
    for link in store.coaches_of(user["id"]):
        if effective_plan({"plan": link["coach_plan"], "plan_expires_at": link["coach_plan_expires_at"]}) == "coach":
            return {**user, "plan": "pro", "plan_expires_at": None, "sponsored_by": link["coach_email"]}
    return user


def optional_user(request: Request) -> dict | None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    return store.session_user(auth.token_hash(token)) if token else None


def verified_user(user: dict = Depends(current_user)) -> dict:
    """Like current_user, but when REQUIRE_EMAIL_VERIFICATION=1 the email must be confirmed first."""
    if settings.require_verified_email and not user.get("email_verified_at"):
        raise HTTPException(403, {"message": "Please confirm your email first: check your inbox for our link.", "verify": True})
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Only the site owner can do that.")
    return user


def owned_account(account_id: int, user: dict) -> dict:
    acct = store.account(account_id)
    if not acct or acct["user_id"] != user["id"]:
        raise HTTPException(404, "No such account.")
    return acct


def require_feature(user: dict, feature: str) -> None:
    if not has_feature(user, feature):
        raise HTTPException(402, {"message": f"{FEATURE_NAMES.get(feature, feature)} is part of Pro.", "upgrade": True, "feature": feature})


def spend(user: dict, kind: str, limit: int) -> None:
    if store.usage_today(user["id"], kind) >= limit:
        upgrade = not is_pro(user)
        raise HTTPException(429, {"message": f"You've used today's {limit} {kind} requests." + (" Pro raises the limit." if upgrade else ""),
                                  "upgrade": upgrade})
    store.bump_usage(user["id"], kind)


def platform_allowed(platform: str) -> None:
    if platform == "chesscom" and not settings.chesscom_enabled:
        raise HTTPException(400, "Chess.com import isn't available on this server yet. Use Lichess or paste a PGN.")


def games_cap(user: dict) -> int:
    """Plan limit, never above the server-wide MAX_GAMES cost cap."""
    return min(plan_of(user)["max_games_per_sync"], settings.max_games)


def games_or_404(acct: dict):
    games = store.load_games(player_key(acct))
    if not games:
        raise HTTPException(404, "No analysed games for this account yet. Sync it first.")
    return games


def bundle_or_404(acct: dict):
    b = importer.knowledge.load(acct)
    if b is None:
        raise HTTPException(404, "No knowledge bundle yet. Sync some games first.")
    return b


def set_session(resp: Response, user_id: int) -> None:
    token, th, expires = auth.new_session_token()
    store.add_session(th, user_id, expires)
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86_400, httponly=True,
                    samesite="lax", secure=settings.cookie_secure, path="/")


def account_view(acct: dict) -> dict:
    return {
        "id": acct["id"], "platform": acct["platform"], "handle": acct["handle"],
        "auto_sync": bool(acct["auto_sync"]), "time_classes": acct["time_classes"].split(","),
        "last_synced_at": acct["last_synced_at"], "last_sync_error": acct["last_sync_error"],
        "games": store.count_games(player_key(acct)), "running_job": queue.active_for_account(acct["id"]),
        "role": acct.get("role") or "own",
    }


def send_verification(user_id: int, email: str, background: BackgroundTasks) -> None:
    token = store.create_email_token(user_id, "verify", VERIFY_TTL_S)
    subject, text, html = verification_email(f"{settings.public_url}/verify#token={token}")
    background.add_task(mailer.send, email, subject, text, html)


# ---- models -----------------------------------------------------------------------
class Credentials(BaseModel):
    email: str = Field(..., max_length=260)
    password: str = Field(..., max_length=200)


class EmailOnly(BaseModel):
    email: str = Field(..., max_length=260)


class TokenBody(BaseModel):
    token: str = Field(..., max_length=200)


class ResetBody(BaseModel):
    token: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


class PasswordBody(BaseModel):
    password: str = Field(..., max_length=200)


class MePatch(BaseModel):
    digest: bool | None = None


class LinkRequest(BaseModel):
    platform: str = Field(..., pattern="^(lichess|chesscom|pgn|demo)$")
    handle: str = Field("", max_length=60)


class SyncRequest(BaseModel):
    max_games: int | None = Field(None, ge=1, le=500)
    time_classes: list[str] | None = None
    full: bool = False


class PgnRequest(BaseModel):
    pgn: str = Field(..., max_length=5_000_000)


class AccountPatch(BaseModel):
    auto_sync: bool


class AttemptRequest(BaseModel):
    move: str = Field(..., max_length=10)
    practice: bool = False  # a retry after a miss: graded, but the review schedule isn't touched
    hinted: bool = False  # solved after a hint: it comes back as if missed


class HintRequest(BaseModel):
    level: int = Field(1, ge=1, le=2)  # 1: the piece to move, 2: the whole move


class AnalyseRequest(BaseModel):
    fen: str = Field(..., max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=40)


class ScoutRequest(BaseModel):
    platform: str = Field(..., pattern="^(lichess|chesscom)$")
    handle: str = Field(..., max_length=30)
    account_id: int | None = None
    max_games: int = Field(100, ge=10, le=200)
    time_classes: list[str] | None = None


class UpgradeRequest(BaseModel):
    note: str | None = Field(None, max_length=500)
    plan: str = Field("pro", max_length=20)


class PreviewRequest(BaseModel):
    username: str = Field(..., max_length=30)
    platform: str = Field("lichess", pattern="^(lichess|chesscom)$")


class SupportRequest(BaseModel):
    email: str | None = Field(None, max_length=260)
    subject: str = Field(..., min_length=2, max_length=140)
    message: str = Field(..., min_length=5, max_length=5000)
    page: str | None = Field(None, max_length=300)


class ClientError(BaseModel):
    message: str = Field("", max_length=1000)
    source: str | None = Field(None, max_length=500)
    line: int | None = None
    col: int | None = None
    stack: str | None = Field(None, max_length=8000)
    page: str | None = Field(None, max_length=500)


# ---- auth -------------------------------------------------------------------------
@app.post("/api/auth/signup")
def signup(body: Credentials, request: Request, response: Response, background: BackgroundTasks) -> dict:
    ip = client_ip(request)
    if signup_limiter.blocked(ip):
        raise HTTPException(429, "Too many new accounts from this network. Try again in an hour.")
    signup_limiter.fail(ip)
    try:
        email = auth.normalize_email(body.email)
        auth.check_password_strength(body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    is_admin = email in admin_emails()
    try:
        uid = store.create_user(email, auth.hash_password(body.password), "pro" if is_admin else "free", is_admin)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    set_session(response, uid)
    send_verification(uid, email, background)
    return {"ok": True, "verify_sent": True}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response) -> dict:
    ip = client_ip(request)
    if login_limiter.blocked(ip):
        raise HTTPException(429, "Too many sign-in attempts. Try again in a few minutes.")
    try:
        email = auth.normalize_email(body.email)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    user = store.user_by_email(email)
    # Always run one scrypt check, so response time doesn't reveal whether an email has an account.
    ok = auth.verify_password(body.password, user["pw_hash"] if user else DUMMY_HASH)
    if not user or not ok:
        login_limiter.fail(ip)
        raise HTTPException(401, "Wrong email or password.")
    login_limiter.reset(ip)
    set_session(response, user["id"])
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        store.delete_session(auth.token_hash(token))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/verify")
def verify_email(body: TokenBody) -> dict:
    uid = store.use_email_token(body.token, "verify")
    if not uid:
        raise HTTPException(400, "That link has expired or was already used. Sign in and ask for a new one.")
    store.update_user(uid, email_verified_at=time.time())
    return {"ok": True, "message": "Email confirmed. Thanks!"}


@app.post("/api/auth/verify/resend")
def resend_verification(background: BackgroundTasks, user: dict = Depends(current_user)) -> dict:
    if user.get("email_verified_at"):
        return {"ok": True, "message": "Your email is already confirmed."}
    key = f"user:{user['id']}"
    if resend_limiter.blocked(key):
        raise HTTPException(429, "We've sent a few links already. Check your spam folder, or try again in an hour.")
    resend_limiter.fail(key)
    send_verification(user["id"], user["email"], background)
    return {"ok": True, "message": f"Sent a new link to {user['email']}."}


@app.post("/api/auth/forgot")
def forgot_password(body: EmailOnly, request: Request, background: BackgroundTasks) -> dict:
    ip = client_ip(request)
    if forgot_limiter.blocked(ip):
        raise HTTPException(429, "Too many reset requests. Try again in an hour.")
    forgot_limiter.fail(ip)
    try:
        email = auth.normalize_email(body.email)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    user = store.user_by_email(email)
    if user:  # same answer either way, so the form can't be used to find out who has an account
        token = store.create_email_token(user["id"], "reset", RESET_TTL_S)
        subject, text, html = reset_email(f"{settings.public_url}/reset#token={token}")
        background.add_task(mailer.send, email, subject, text, html)
    return {"ok": True, "message": "If there's an account for that email, we've sent a link to reset the password. It works for one hour."}


@app.post("/api/auth/reset")
def reset_password(body: ResetBody, response: Response) -> dict:
    try:
        auth.check_password_strength(body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    uid = store.use_email_token(body.token, "reset")
    if not uid:
        raise HTTPException(400, "That reset link has expired or was already used. Ask for a new one.")
    user = store.user_by_id(uid)
    store.update_user(uid, pw_hash=auth.hash_password(body.password), email_verified_at=user.get("email_verified_at") or time.time())
    store.delete_sessions(uid)  # sign out everywhere else
    set_session(response, uid)
    return {"ok": True, "message": "Password changed. You're signed in."}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    p = plan_of(user)
    plan_id = effective_plan(user)
    return {
        "email": user["email"], "plan": plan_id, "plan_name": p["name"], "is_admin": bool(user["is_admin"]),
        "plan_expires_at": user.get("plan_expires_at") if plan_id != "free" else None,
        "email_verified": bool(user.get("email_verified_at")), "digest": bool(user.get("digest_opt_in", 1)),
        "sponsored_by": user.get("sponsored_by"), "max_students": p.get("max_students", 0),
        "coaches": [{"student_id": c["id"], "coach_email": c["coach_email"]} for c in store.coaches_of(user["id"])],
        "features": sorted(p["features"]),
        "limits": {k: p[k] for k in ("max_games_per_sync", "max_accounts", "coach_messages_per_day", "scouts_per_day")},
        "usage_today": {"coach": store.usage_today(user["id"], "coach"), "scout": store.usage_today(user["id"], "scout")},
        "accounts": [account_view(a) for a in store.accounts(user["id"])],
    }


@app.patch("/api/me")
def patch_me(body: MePatch, user: dict = Depends(current_user)) -> dict:
    if body.digest is not None:
        store.update_user(user["id"], digest_opt_in=int(body.digest))
    return {"ok": True}


@app.delete("/api/me")
def delete_me(body: PasswordBody, response: Response, user: dict = Depends(current_user)) -> dict:
    """Delete the account and everything in it (games, puzzles, bundles, repertoires). Needs the password."""
    if not auth.verify_password(body.password, user["pw_hash"]):
        raise HTTPException(401, "That password isn't right.")
    for acct in store.delete_user(user["id"]):
        importer.knowledge.remove(acct)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/plans")
def plans() -> list[dict]:
    return public_plans()


@app.post("/api/upgrade-request")
def upgrade_request(body: UpgradeRequest, user: dict = Depends(current_user)) -> dict:
    if body.plan not in OFFER_IDS or body.plan == "free":
        raise HTTPException(400, "Unknown plan.")
    store.add_upgrade_request(user["id"], body.note, body.plan)
    msg = {"coach": "Thanks! We'll email you when your Coach plan is switched on.",
           "event": f"Thanks! We'll email you when your {EVENT_PASS_DAYS}-day Event Pass is switched on."}.get(
        body.plan, "Thanks! We'll email you when your Pro access is switched on.")
    return {"ok": True, "message": msg}


# ---- instant preview (no account) ---------------------------------------------------------
@app.post("/api/preview")
def start_preview(body: PreviewRequest, request: Request) -> dict:
    platform_allowed(body.platform)
    try:
        name = validate_username(body.username)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    key = f"{body.platform}:{name.lower()}"
    hit = queue.find_recent("preview", key, PREVIEW_CACHE_S)
    if hit:
        return {"job_id": hit["id"], "cached": True}
    ip = client_ip(request)
    if preview_limiter.blocked(ip):
        raise HTTPException(429, "You've run several previews in the last hour. Create a free account to see full reports.")
    preview_limiter.fail(ip)
    return {"job_id": jobs.submit("preview", {"platform": body.platform, "username": name, "key": key}), "cached": False}


@app.get("/api/preview/{job_id}")
def preview_status(job_id: str) -> dict:
    v = queue.view(job_id, None)
    if not v or v["kind"] != "preview":
        raise HTTPException(404, "No such preview.")
    return v


# ---- linked accounts & imports ---------------------------------------------------------
@app.post("/api/accounts")
def link_account(body: LinkRequest, user: dict = Depends(current_user)) -> dict:
    existing = [a for a in store.accounts(user["id"]) if (a.get("role") or "own") == "own"]
    if len(existing) >= plan_of(user)["max_accounts"]:
        raise HTTPException(402, {"message": f"Your plan allows {plan_of(user)['max_accounts']} linked accounts.", "upgrade": not is_pro(user)})
    platform_allowed(body.platform)
    if body.platform == "demo":
        handle = DEMO_HANDLE
    elif body.platform == "pgn":
        try:
            handle = validate_pgn_name(body.handle)
        except GameImportError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        try:
            handle = check_account(body.platform, body.handle)
        except GameImportError as exc:
            raise HTTPException(400, str(exc)) from exc
    return account_view(store.add_account(user["id"], body.platform, handle))


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: int, body: AccountPatch, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    if body.auto_sync:
        require_feature(user, "autosync")
        if acct["platform"] not in ("lichess", "chesscom"):
            raise HTTPException(400, "Auto-sync works for Lichess and Chess.com accounts.")
    store.update_account(account_id, auto_sync=int(body.auto_sync))
    return account_view(store.account(account_id))


@app.delete("/api/accounts/{account_id}")
def unlink_account(account_id: int, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    store.delete_account(account_id)  # first, so a running import stops at its next chunk
    store.delete_player(player_key(acct))
    importer.knowledge.remove(acct)
    return {"ok": True}


def _start_import(user: dict, acct: dict, **payload) -> dict:
    job_id = jobs.submit("import", payload, user["id"], acct["id"], exclusive=True)  # atomic: no double imports
    if job_id is None:
        raise HTTPException(409, "This account is already syncing.")
    return {"job_id": job_id}


@app.post("/api/accounts/{account_id}/sync")
def sync_account(account_id: int, body: SyncRequest, user: dict = Depends(verified_user)) -> dict:
    acct = owned_account(account_id, user)
    if acct["platform"] == "pgn":
        raise HTTPException(400, "This is a PGN account. Upload or paste new games to add them.")
    platform_allowed(acct["platform"])
    cap = games_cap(user)
    classes = [c for c in (body.time_classes or acct["time_classes"].split(",")) if c in TIME_CLASSES]
    if not classes:
        raise HTTPException(400, "Pick at least one time control.")
    store.update_account(account_id, time_classes=",".join(classes))
    want = min(body.max_games or cap, cap)
    # Asking for more games than are already analysed means "go back through my history", not just "anything new".
    backfill = body.full or want > store.count_games(player_key(acct))
    return _start_import(user, acct, max_games=want, time_classes=classes, full=backfill)


@app.post("/api/accounts/{account_id}/pgn")
def import_pgn(account_id: int, body: PgnRequest, user: dict = Depends(verified_user)) -> dict:
    acct = owned_account(account_id, user)
    if not body.pgn.strip():
        raise HTTPException(400, "Paste a PGN to import.")
    return _start_import(user, acct, max_games=games_cap(user), pgn=body.pgn)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: dict = Depends(current_user)) -> dict:
    job = queue.view(job_id, user["id"])
    if not job:
        raise HTTPException(404, "No such job.")
    return job


# ---- player views -----------------------------------------------------------------------
@app.get("/api/accounts/{account_id}/profile")
def profile(account_id: int, rating: int | None = None, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    games = games_or_404(acct)
    dna = rating_dna(games, rating)
    recent = sorted(games, key=lambda g: g.played_at, reverse=True)[:12]
    return {
        "account": account_view(acct),
        "dna": dna,
        "insights": insights(games, dna),
        "due_puzzles": store.count_due(player_key(acct)),
        "recent_games": [
            {"game_id": g.game_id, "opponent": g.opponent, "result": g.result, "score": g.score, "color": g.player_color,
             "accuracy": g.accuracy, "opening": g.opening, "played_at": g.played_at, "time_class": g.time_class, "url": g.url}
            for g in recent
        ],
    }


@app.get("/api/accounts/{account_id}/game/{game_id}")
def game_detail(account_id: int, game_id: str, user: dict = Depends(current_user)) -> dict:
    """One analysed game for the interactive board: every move, plus the engine's view of the player's mistakes."""
    acct = owned_account(account_id, user)
    g = next((g for g in store.load_games(player_key(acct)) if g.game_id == game_id), None)
    if not g:
        raise HTTPException(404, "No such game.")
    marks = [{"ply": m.ply, "classification": m.classification, "played": m.played_san, "best": m.best_san, "best_uci": m.best,
              "played_uci": m.played, "line": m.best_line[:5], "win_loss": round(m.win_loss, 1), "fen": m.fen_before}
             for m in g.moves if m.classification in ("inaccuracy", "mistake", "blunder")]
    board, fens, ucis = chess.Board(), [chess.STARTING_FEN], []
    for san in g.moves_san:  # replay on the server so the browser needs no chess rules
        try:
            mv = board.parse_san(san)
        except ValueError:
            break
        ucis.append(mv.uci())
        board.push(mv)
        fens.append(board.fen())
    return {"game_id": g.game_id, "opponent": g.opponent, "color": g.player_color, "result": g.result, "opening": g.opening,
            "played_at": g.played_at, "accuracy": g.accuracy, "url": g.url, "time_class": g.time_class,
            "moves_san": g.moves_san[: len(ucis)], "ucis": ucis, "fens": fens, "marks": marks}


@app.get("/api/accounts/{account_id}/plan")
def plan(account_id: int, minutes: int = 45, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    games = games_or_404(acct)
    return weekly_plan(rating_dna(games), store.count_due(player_key(acct)), max(15, min(minutes, 180)))


@app.get("/api/accounts/{account_id}/puzzles")
def puzzles(account_id: int, due: bool = True, limit: int = 20, user: dict = Depends(current_user)) -> list[dict]:
    acct = owned_account(account_id, user)
    key = player_key(acct)
    rows = store.puzzles(key, due_only=due, limit=max(1, min(limit, 100)))
    _add_puzzle_context(key, rows)
    for r in rows:  # don't ship the answer to the browser before the attempt
        for k in ("solution", "solution_san", "line", "accept"):
            r.pop(k, None)
    return rows


def _add_puzzle_context(key: str, rows: list[dict]) -> None:
    """The opponent's move that led to each puzzle (so the board can play it in first), and which game it's from."""
    games = store.games_meta(key, {r["game_id"] for r in rows if not r["game_id"].startswith("prep-")})
    for r in rows:
        g = games.get(r["game_id"])
        if not g:
            continue
        r.update(opponent=g.get("opponent"), played_at=g.get("played_at"), game_url=g.get("url"), time_class=g.get("time_class"))
        sans = g.get("moves_san") or []
        if r["ply"] < 1 or len(sans) < r["ply"]:
            continue
        board = chess.Board()
        try:
            for san in sans[: r["ply"] - 1]:
                board.push_san(san)
            prev_fen = board.fen()
            mv = board.parse_san(sans[r["ply"] - 1])
            board.push(mv)
        except ValueError:  # a game from a set-up position, or an unreadable move
            continue
        if board.board_fen() == r["fen"].split(" ")[0]:
            r.update(prev_fen=prev_fen, prev_uci=mv.uci())


def _owned_puzzle(puzzle_id: int, user: dict) -> dict:
    p = store.get_puzzle(puzzle_id)
    if not p or p["username"] not in {player_key(a) for a in store.accounts(user["id"])}:
        raise HTTPException(404, "No such puzzle.")
    return p


@app.post("/api/puzzles/{puzzle_id}/hint")
def puzzle_hint(puzzle_id: int, body: HintRequest, user: dict = Depends(current_user)) -> dict:
    p = _owned_puzzle(puzzle_id, user)
    return {"from": p["solution"][:2], "to": p["solution"][2:4] if body.level >= 2 else None}


class RevealRequest(BaseModel):
    practice: bool = False


@app.post("/api/puzzles/{puzzle_id}/reveal")
def puzzle_reveal(puzzle_id: int, body: RevealRequest, user: dict = Depends(current_user)) -> dict:
    """Show the solution without answering: counts as a miss unless it's a practice run."""
    p = _owned_puzzle(puzzle_id, user)
    srs = {"practice": True} if body.practice else store.record_attempt(puzzle_id, False)
    return {"solution": p["solution"], "solution_san": p["solution_san"], "line": p["line"], "kind": p.get("kind", "mistake"), **srs}


@app.post("/api/engine/analyse")
def engine_analyse(body: AnalyseRequest, user: dict = Depends(current_user)) -> dict:
    """Explore mode: the engine's view of any position the player reaches."""
    if state["engine"] is None:
        raise HTTPException(503, "The engine isn't running on this server right now.")
    key = f"user:{user['id']}"
    if analyse_limiter.blocked(key):
        raise HTTPException(429, "That's a lot of engine look-ups. Take a breather and try again in a while.")
    analyse_limiter.fail(key)
    try:
        board = chess.Board(body.fen)
    except ValueError as exc:
        raise HTTPException(400, "That position isn't valid.") from exc
    if not board.is_valid():
        raise HTTPException(400, "That position isn't valid.")
    if board.is_game_over():
        outcome = board.outcome()
        return {"over": True, "result": outcome.result() if outcome else "*", "cp": None, "mate": None, "best": None, "line": []}
    ev = state["engine"].evaluate(board, chess.engine.Limit(depth=18, time=0.35))
    line, b = [], board.copy()
    for u in ev.pv[:6]:
        mv = chess.Move.from_uci(u)
        if mv not in b.legal_moves:
            break
        line.append(b.san(mv))
        b.push(mv)
    return {"over": False, "cp": ev.cp_white if ev.mate_white is None else None, "mate": ev.mate_white,
            "best": ev.best_move, "best_san": line[0] if line else None, "line": line}


@app.post("/api/puzzles/{puzzle_id}/attempt")
def attempt(puzzle_id: int, req: AttemptRequest, user: dict = Depends(current_user)) -> dict:
    p = _owned_puzzle(puzzle_id, user)
    board = chess.Board(p["fen"])
    if p.get("kind") == "prep":
        p["accept_san"] = [board.san(chess.Move.from_uci(u)) for u in p.get("accept", []) if chess.Move.from_uci(u) in board.legal_moves]
    try:
        move = chess.Move.from_uci(req.move)
    except ValueError:
        try:
            move = board.parse_san(req.move)
        except chess.IllegalMoveError as exc:
            raise HTTPException(400, f"{req.move} isn't a legal move here.") from exc
        except chess.AmbiguousMoveError as exc:
            raise HTTPException(400, f"{req.move} could mean more than one move. Add the starting file or rank, e.g. Nbd2.") from exc
        except ValueError as exc:
            raise HTTPException(400, f"Couldn't read '{req.move}'. Type it like Nf3, exd5, O-O or e8=Q.") from exc
    if move not in board.legal_moves and move.promotion is None:
        promo = chess.Move(move.from_square, move.to_square, chess.QUEEN)
        move = promo if promo in board.legal_moves else move
    if move not in board.legal_moves:
        raise HTTPException(400, "That move isn't legal here.")

    verdict, note = "wrong", None
    if move.uci() == p["solution"] or move.uci() in p.get("accept", []):
        verdict = "correct"
        if move.uci() != p["solution"]:
            note = "That's in your preparation too."
    elif p.get("kind") == "prep":
        note = f"Your preparation here: {', '.join(p['accept_san'])}." if p.get("accept_san") else None
    elif state["engine"] is not None:
        side = board.turn
        best = state["engine"].evaluate(board)
        after_board = board.copy()
        after_board.push(move)
        after = state["engine"].evaluate(after_board)
        if win_percent(best.cp_for(side)) - win_percent(after.cp_for(side)) <= ALSO_GOOD_WIN_PCT:
            verdict, note = "also_good", "Not the engine's first choice, but just as good."
    srs = {"practice": True} if req.practice else store.record_attempt(puzzle_id, verdict != "wrong" and not req.hinted)
    return {"verdict": verdict, "note": note, "your_move": board.san(move), "your_uci": move.uci(), "solution": p["solution"],
            "kind": p.get("kind", "mistake"), "solution_san": p["solution_san"], "line": p["line"], "you_played_in_game": p["played_san"], **srs}


@app.post("/api/accounts/{account_id}/coach")
def coach(account_id: int, req: ChatRequest, user: dict = Depends(verified_user)) -> dict:
    acct = owned_account(account_id, user)
    games = games_or_404(acct)
    spend(user, "coach", plan_of(user)["coach_messages_per_day"])
    tools = CoachTools(games, bundle_or_404(acct), state["engine"], store.count_due(player_key(acct)), pro=is_pro(user))
    backends = resolve_backends(settings)
    r = Coach(acct["handle"], tools, backends).chat(req.message, req.history)  # never raises: falls back to offline
    coach_stats.record(r.mode, r.notice)
    return {"reply": r.reply, "answer": r.answer, "notice": r.notice, "mode": r.mode, "tools_used": r.tools_used,
            "model": r.model or (backends[0].label if backends else None), "citations": r.citations, "grounded": r.grounded,
            "unverified": r.unverified}


# ---- Pro --------------------------------------------------------------------------------
@app.get("/api/accounts/{account_id}/teasers")
def teasers(account_id: int, user: dict = Depends(current_user)) -> dict:
    """Counts only, so the locked Pro screens can say what they found ('3 leaks') without giving it away."""
    acct = owned_account(account_id, user)
    games = games_or_404(acct)
    rep = repertoire_report(games)
    prog = progress_report(games)
    trend = prog.get("trend") or {}
    change = trend.get("accuracy_change")
    return {"leaks": len(rep["leaks"]), "recurring_mistakes": len(rep["recurring_mistakes"]),
            "lines": len(rep["white"]) + len(rep["black"]), "progress_periods": len(prog["buckets"]),
            "accuracy_trend": None if change is None else ("up" if change > 0 else "down" if change < 0 else "flat"),
            "repertoires": len(store.repertoires(account_id))}


@app.get("/api/accounts/{account_id}/repertoire")
def repertoire(account_id: int, user: dict = Depends(current_user)) -> dict:
    require_feature(user, "repertoire")
    return repertoire_report(games_or_404(owned_account(account_id, user)))


@app.get("/api/accounts/{account_id}/progress")
def progress(account_id: int, user: dict = Depends(current_user)) -> dict:
    require_feature(user, "progress")
    return progress_report(games_or_404(owned_account(account_id, user)))


@app.post("/api/scout")
def scout(req: ScoutRequest, user: dict = Depends(verified_user)) -> dict:
    require_feature(user, "scout")
    platform_allowed(req.platform)
    my_games = None
    if req.account_id is not None:
        my_games = store.load_games(player_key(owned_account(req.account_id, user)))
    limit = plan_of(user)["scouts_per_day"]
    if store.usage_today(user["id"], "scout") >= limit:
        spend(user, "scout", limit)  # raises the usual "daily limit reached" error
    try:
        handle = check_account(req.platform, req.handle)
        classes = req.time_classes or ["bullet", "blitz", "rapid", "classical"]
        games = fetch_games(req.platform, handle, req.max_games, classes)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    spend(user, "scout", limit)  # only successful downloads count towards the daily limit
    return scout_report(handle, games, state["engine"], my_games)


# ---- file upload (ChessBase / any PGN) -------------------------------------------------------
@app.post("/api/accounts/{account_id}/upload")
async def upload_games(account_id: int, file: UploadFile = File(...), user: dict = Depends(verified_user)) -> dict:
    """Upload a .pgn (or .zip of .pgn) exported from ChessBase or any other tool."""
    acct = owned_account(account_id, user)
    if acct["platform"] != "pgn":
        raise HTTPException(400, "Uploads go to a 'PGN / ChessBase' account. Link one first.")
    try:
        text = decode_upload(await file.read(), file.filename or "")
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    names = player_names(text, 40)
    if not any(name_matches(n, acct["handle"]) for n, _ in names):
        top = ", ".join(f"{n} ({c})" for n, c in names[:6]) or "none"
        raise HTTPException(400, {"message": f"No games for '{acct['handle']}' in that file. Names found: {top}. "
                                             "Link a PGN account with your name exactly as it appears.",
                                  "names": [n for n, _ in names[:10]]})
    return _start_import(user, acct, max_games=games_cap(user), pgn=text)


# ---- Prep Check (Pro): ChessBase repertoire vs games ---------------------------------------
@app.post("/api/accounts/{account_id}/repertoires")
async def upload_repertoire(account_id: int, file: UploadFile = File(...), color: str = Form("auto"), name: str = Form(""),
                            user: dict = Depends(verified_user)) -> dict:
    require_feature(user, "prep")
    acct = owned_account(account_id, user)
    if not store.load_games(player_key(acct)):
        raise HTTPException(400, "Sync or upload your games first, so there's something to compare the repertoire with.")
    if len(store.repertoires(account_id)) >= 6:
        raise HTTPException(400, "You can keep up to 6 repertoire files per account. Delete one first.")
    try:
        text = decode_upload(await file.read(), file.filename or "")
        side = {"white": chess.WHITE, "black": chess.BLACK}.get(color) if color != "auto" else detect_color(text)
        if side is None:
            raise GameImportError("Colour must be white, black or auto.")
        parse_repertoire(text, side)  # validate now, so errors show immediately
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    label = (name or Path(file.filename or "repertoire").stem)[:60]
    rep_id = store.add_repertoire(user["id"], account_id, label, "white" if side == chess.WHITE else "black", text)
    rep = store.repertoire(rep_id)
    job_id = jobs.submit("prep", {"repertoire_id": rep_id}, user["id"], account_id)
    return {"job_id": job_id, "repertoire_id": rep_id, "color": rep["color"]}


@app.get("/api/accounts/{account_id}/repertoires")
def list_repertoires(account_id: int, user: dict = Depends(current_user)) -> list[dict]:
    require_feature(user, "prep")
    owned_account(account_id, user)
    return [{k: v for k, v in r.items() if k != "pgn"} for r in store.repertoires(account_id)]


@app.delete("/api/repertoires/{rep_id}")
def delete_repertoire(rep_id: int, user: dict = Depends(current_user)) -> dict:
    rep = store.repertoire(rep_id)
    if not rep or rep["user_id"] != user["id"]:
        raise HTTPException(404, "No such repertoire.")
    acct = owned_account(rep["account_id"], user)
    store.delete_prep_drills(player_key(acct), rep_id)
    store.delete_repertoire(rep_id)
    importer.knowledge.rebuild(acct, f"**Deprecation**: Removed repertoire '{rep['name']}'.")
    return {"ok": True}


# ---- OKF knowledge bundle ----------------------------------------------------------------------
@app.get("/api/accounts/{account_id}/knowledge")
def knowledge(account_id: int, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    b = bundle_or_404(acct)
    pro = is_pro(user)
    visible = [c for c in b.concepts.values() if pro or not c.id.startswith(PRO_PREFIXES)]
    by_type: dict[str, list[dict]] = {}
    for c in sorted(visible, key=lambda c: c.id):
        by_type.setdefault(c.type, []).append(c.summary())
    tiers: dict[str, int] = {}
    for c in visible:
        tiers[c.trust] = tiers.get(c.trust, 0) + 1
    return {"okf_version": "0.2", "concepts": len(visible), "types": by_type, "trust": tiers,
            "conformance_problems": b.validate(), "log": b.logs.get("/", "")[:4000],
            "hidden_pro_concepts": 0 if pro else len(b.concepts) - len(visible)}


@app.get("/api/accounts/{account_id}/concept")
def concept(account_id: int, id: str, user: dict = Depends(current_user)) -> dict:
    acct = owned_account(account_id, user)
    b = bundle_or_404(acct)
    c = b.get(id)
    if not c or (not is_pro(user) and c.id.startswith(PRO_PREFIXES)):
        raise HTTPException(404, "No such concept.")
    return {**c.summary(), "frontmatter": c.frontmatter, "body": c.body, "markdown": c.render(),
            "links": [{"label": lbl, "id": t, "exists": b.exists(t)} for lbl, t in c.links()], "backlinks": b.backlinks(c.id)[:20]}


@app.get("/api/accounts/{account_id}/bundle.zip")
def export_bundle(account_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    """The player's OKF bundle with the principles bundle at /knowledge, as one portable zip."""
    acct = owned_account(account_id, user)
    bundle_or_404(acct)
    root = importer.knowledge.path(acct)
    pro = is_pro(user)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(root).as_posix()
            if not pro and ("/" + rel).startswith(PRO_PREFIXES):
                continue
            z.write(p, rel)
        for p in sorted(KNOWLEDGE_DIR.rglob("*.md")):
            z.write(p, "knowledge/" + p.relative_to(KNOWLEDGE_DIR).as_posix())
    buf.seek(0)
    fname = f"okf-{acct['platform']}-{acct['handle']}.zip"
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---- coach dashboard ------------------------------------------------------------------------
class StudentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    platform: str = Field(..., pattern="^(lichess|chesscom|pgn)$")
    handle: str = Field(..., min_length=1, max_length=60)


class StudentPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    note: str | None = Field(None, max_length=4000)


class InviteRequest(BaseModel):
    email: str | None = Field(None, max_length=200)


class AssignmentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=140)
    detail: str = Field("", max_length=2000)
    skill: str | None = Field(None, max_length=20)
    due_days: int | None = Field(None, ge=1, le=60)


class AssignmentPatch(BaseModel):
    done: bool


class JoinRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=100)


def coach_user(user: dict = Depends(current_user)) -> dict:
    if not is_coach(user):
        raise HTTPException(402, {"message": "The coach dashboard is part of the Coach plan.", "upgrade": True})
    return user


def owned_student(student_id: int, user: dict) -> dict:
    st = store.student(student_id)
    if not st or st["coach_id"] != user["id"]:
        raise HTTPException(404, "No such student.")
    return st


def _skill_view(dna: dict) -> dict:
    base = dna["base_rating"]
    return {s["key"]: {"label": s["label"], "rating": s["rating"], "offset": s["rating"] - base, "accuracy": s["accuracy"],
                       "low_confidence": s["low_confidence"]} for s in dna["skills"]}


def student_summary(st: dict) -> dict:
    acct = store.account(st["account_id"]) or {}
    out = {"id": st["id"], "name": st["name"], "note": st.get("note") or "", "account_id": st["account_id"],
           "platform": acct.get("platform"), "handle": acct.get("handle"), "joined": bool(st.get("student_user_id")),
           "student_email": st.get("student_email"), "games": 0, "base_rating": None, "skills": {}, "weakest": [],
           "accuracy": None, "accuracy_trend": None, "form": None, "last_synced_at": acct.get("last_synced_at"),
           "last_sync_error": acct.get("last_sync_error"), "running_job": queue.active_for_account(st["account_id"]) if acct else None}
    rows = store.assignments(st["id"])
    out["homework_open"] = sum(1 for a in rows if not a["done_at"])
    out["homework_done"] = sum(1 for a in rows if a["done_at"])
    if not acct:
        return out
    games = store.load_games(player_key(acct))
    if not games:
        return out
    dna = rating_dna(games)
    skills = _skill_view(dna)
    recent = sorted(games, key=lambda g: g.played_at, reverse=True)
    last, prev = recent[:20], recent[20:40]
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    acc_last, acc_prev = mean([g.accuracy for g in last]), mean([g.accuracy for g in prev])
    out.update(games=len(games), base_rating=dna["base_rating"], rating_is_estimated=dna["rating_is_estimated"],
               accuracy=dna["overall_accuracy"], skills=skills,
               weakest=[k for k, v in sorted(skills.items(), key=lambda kv: kv[1]["offset"]) if not v["low_confidence"]][:2],
               accuracy_trend=round(acc_last - acc_prev, 1) if acc_last is not None and acc_prev is not None else None,
               form=round(100 * sum(g.score for g in last) / len(last)) if last else None,
               due_puzzles=store.count_due(player_key(acct)))
    return out


@app.get("/api/coach/students")
def coach_students(user: dict = Depends(coach_user)) -> dict:
    rows = [student_summary(st) for st in store.students(user["id"])]
    squad: dict[str, list[int]] = {}
    for r in rows:
        for k, v in r["skills"].items():
            if not v["low_confidence"]:
                squad.setdefault(k, []).append(v["offset"])
    labels = {k: label for k, (label, _) in SKILLS.items()}
    squad_view = sorted(({"key": k, "label": labels.get(k, k), "avg_offset": round(sum(v) / len(v)), "students": len(v)}
                         for k, v in squad.items()), key=lambda x: x["avg_offset"])
    return {"students": rows, "squad": squad_view, "max_students": plan_of(user).get("max_students", 0),
            "skills": [{"key": k, "label": label} for k, (label, _) in SKILLS.items()],
            "games_per_student": games_cap(user)}


@app.post("/api/coach/students")
def add_student(body: StudentCreate, user: dict = Depends(coach_user)) -> dict:
    cap = plan_of(user).get("max_students", 0)
    if len(store.students(user["id"])) >= cap:
        raise HTTPException(402, {"message": f"Your plan has room for {cap} students. Remove one to add another.", "upgrade": False})
    if body.platform != "pgn":
        platform_allowed(body.platform)
    try:
        handle = validate_pgn_name(body.handle) if body.platform == "pgn" else check_account(body.platform, body.handle)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    if any(a["platform"] == body.platform and a["handle"].lower() == handle.lower() for a in store.accounts(user["id"])):
        raise HTTPException(409, f"{handle} is already in your accounts or on your roster.")
    acct = store.add_account(user["id"], body.platform, handle, role="student")
    sid = store.add_student(user["id"], acct["id"], body.name.strip())
    if body.platform != "pgn":
        jobs.submit("import", {"max_games": games_cap(user), "time_classes": acct["time_classes"].split(",")}, user["id"], acct["id"], exclusive=True)
    return student_summary(store.student(sid))


@app.get("/api/coach/students/{student_id}")
def get_student(student_id: int, user: dict = Depends(coach_user)) -> dict:
    st = owned_student(student_id, user)
    out = student_summary(st)
    acct = store.account(st["account_id"])
    games = store.load_games(player_key(acct)) if acct else []
    if games:
        dna = rating_dna(games)
        out["dna"] = dna
        out["insights"] = insights(games, dna)[:5]
        out["recent_games"] = [{"game_id": g.game_id, "opponent": g.opponent, "result": g.result, "score": g.score, "color": g.player_color,
                                "accuracy": g.accuracy, "opening": g.opening, "played_at": g.played_at, "time_class": g.time_class}
                               for g in sorted(games, key=lambda g: g.played_at, reverse=True)[:10]]
    out["assignments"] = store.assignments(student_id)
    out["invite_pending"] = bool(st.get("invite_hash")) and (st.get("invite_expires") or 0) > time.time()
    return out


@app.patch("/api/coach/students/{student_id}")
def patch_student(student_id: int, body: StudentPatch, user: dict = Depends(coach_user)) -> dict:
    owned_student(student_id, user)
    store.update_student(student_id, **body.model_dump(exclude_none=True))
    return {"ok": True}


@app.delete("/api/coach/students/{student_id}")
def remove_student(student_id: int, user: dict = Depends(coach_user)) -> dict:
    st = owned_student(student_id, user)
    acct = store.account(st["account_id"])
    store.delete_student(student_id)
    if acct:
        store.delete_account(acct["id"])
        store.delete_player(player_key(acct))
        importer.knowledge.remove(acct)
    return {"ok": True}


@app.post("/api/coach/students/{student_id}/sync")
def sync_student(student_id: int, user: dict = Depends(coach_user)) -> dict:
    st = owned_student(student_id, user)
    acct = store.account(st["account_id"])
    if not acct or acct["platform"] == "pgn":
        raise HTTPException(400, "This student's games come from PGN uploads. Upload new games to add them.")
    return _start_import(user, acct, max_games=games_cap(user), time_classes=acct["time_classes"].split(","))


@app.post("/api/coach/sync-all")
def sync_all_students(user: dict = Depends(coach_user)) -> dict:
    queued = 0
    for st in store.students(user["id"]):
        acct = store.account(st["account_id"])
        live = acct and acct["platform"] in ("lichess", "chesscom") and settings_allows(acct["platform"])
        if live and jobs.submit("import", {"max_games": games_cap(user), "time_classes": acct["time_classes"].split(",")},
                                user["id"], acct["id"], exclusive=True):
            queued += 1
    return {"queued": queued, "message": f"Syncing {queued} student{'s' if queued != 1 else ''}. Each one updates as it finishes."}


def settings_allows(platform: str) -> bool:
    return platform != "chesscom" or settings.chesscom_enabled


@app.post("/api/coach/students/{student_id}/invite")
def invite_student(student_id: int, body: InviteRequest, background: BackgroundTasks, user: dict = Depends(coach_user)) -> dict:
    st = owned_student(student_id, user)
    token = store.create_invite(student_id)
    link = f"{settings.public_url}/join#coach={token}"
    sent_to = None
    if body.email:
        try:
            to = auth.normalize_email(body.email)
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        subject, text, html = coach_invite_email(user["email"], st["name"], link)
        background.add_task(mailer.send, to, subject, text, html, user["email"])
        sent_to = to
    return {"link": link, "sent_to": sent_to, "expires_in_days": 14}


@app.post("/api/coach/join")
def join_coach(body: JoinRequest, user: dict = Depends(current_user)) -> dict:
    st = store.accept_invite(body.token, user["id"])
    if not st:
        raise HTTPException(400, "That invite has expired or was already used. Ask your coach for a new link.")
    if st["coach_id"] == user["id"]:
        store.leave_coach(user["id"], st["id"])
        raise HTTPException(400, "That's your own invite link. Send it to your student.")
    coach = store.user_by_id(st["coach_id"])
    return {"ok": True, "message": f"You've joined {coach['email'] if coach else 'your coach'}'s squad. Pro is switched on for you."}


@app.get("/api/me/coaching")
def my_coaching(user: dict = Depends(current_user)) -> list[dict]:
    out = []
    for c in store.coaches_of(user["id"]):
        out.append({"student_id": c["id"], "coach_email": c["coach_email"],
                    "assignments": [{k: a[k] for k in ("id", "title", "detail", "skill", "due_at", "done_at", "created_at")}
                                    for a in store.assignments(c["id"])]})
    return out


@app.delete("/api/me/coaching/{student_id}")
def leave_coach(student_id: int, user: dict = Depends(current_user)) -> dict:
    if not store.leave_coach(user["id"], student_id):
        raise HTTPException(404, "You're not in that squad.")
    return {"ok": True}


@app.post("/api/coach/students/{student_id}/assignments")
def add_assignment(student_id: int, body: AssignmentCreate, user: dict = Depends(coach_user)) -> dict:
    owned_student(student_id, user)
    if body.skill and body.skill not in SKILLS:
        raise HTTPException(400, "Unknown skill.")
    due = time.time() + body.due_days * 86_400 if body.due_days else None
    aid = store.add_assignment(student_id, user["id"], body.title.strip(), body.detail.strip(), body.skill, due)
    return store.assignment(aid)


def _assignment_access(assignment_id: int, user: dict) -> tuple[dict, bool]:
    """(assignment, is_coach): the coach who set it, or the student it's for."""
    a = store.assignment(assignment_id)
    if not a:
        raise HTTPException(404, "No such homework.")
    if a["coach_id"] == user["id"]:
        return a, True
    st = store.student(a["student_id"])
    if st and st.get("student_user_id") == user["id"]:
        return a, False
    raise HTTPException(404, "No such homework.")


@app.patch("/api/assignments/{assignment_id}")
def patch_assignment(assignment_id: int, body: AssignmentPatch, user: dict = Depends(current_user)) -> dict:
    _assignment_access(assignment_id, user)
    store.set_assignment_done(assignment_id, body.done)
    return store.assignment(assignment_id)


@app.delete("/api/assignments/{assignment_id}")
def delete_assignment(assignment_id: int, user: dict = Depends(current_user)) -> dict:
    _, coach = _assignment_access(assignment_id, user)
    if not coach:
        raise HTTPException(403, "Only your coach can remove homework.")
    store.delete_assignment(assignment_id)
    return {"ok": True}


# ---- support, monitoring, config, status --------------------------------------------------------
@app.post("/api/support")
def contact_support(body: SupportRequest, request: Request, background: BackgroundTasks,
                    user: dict | None = Depends(optional_user)) -> dict:
    ip = client_ip(request)
    if support_limiter.blocked(ip):
        raise HTTPException(429, "You've sent several messages already. We'll reply to those first.")
    try:
        email = user["email"] if user else auth.normalize_email(body.email or "")
    except auth.AuthError as exc:
        raise HTTPException(400, "Add your email so we can reply.") from exc
    support_limiter.fail(ip)
    context = f"User: {'#' + str(user['id']) + ' (' + effective_plan(user) + ')' if user else 'not signed in'} · Page: {body.page or '-'}"
    to = settings.support_email
    store.add_support_message(user["id"] if user else None, email, body.subject, body.message, context, emailed=bool(to))
    if to:
        subject, text = support_email(email, body.subject, body.message, context)
        background.add_task(mailer.send, to, subject, text, None, email)
    return {"ok": True, "message": f"Thanks, we've got your message and will reply to {email}."}


@app.get("/api/admin/email")
def admin_email(user: dict = Depends(admin_user)) -> dict:
    """How this server sends email, any settings that will stop it working, and the last result."""
    return mailer.describe()


@app.post("/api/admin/email/test")
def admin_email_test(user: dict = Depends(admin_user)) -> dict:
    """Send a test email to the owner now; on failure, say what to change."""
    key = f"user:{user['id']}"
    if test_email_limiter.blocked(key):
        raise HTTPException(429, "That's a lot of test emails. Try again in an hour.")
    test_email_limiter.fail(key)
    try:
        info = mailer.test(user["email"])
    except MailError as exc:
        return {"ok": False, "error": str(exc), **mailer.describe()}
    msg = (f"Sent a test email to {user['email']}. It usually arrives within a minute; check spam too."
           if mailer.configured else "Email isn't set up, so the test was saved to the outbox/ folder instead.")
    return {"ok": True, "message": msg, **info}


@app.post("/api/client-error")
def client_error(body: ClientError, request: Request) -> Response:
    ip = client_ip(request)
    if not client_error_limiter.blocked(ip):
        client_error_limiter.fail(ip)
        capture_client_error({**body.model_dump(), "ua": request.headers.get("user-agent", "")[:200]})
    return Response(status_code=204)


@app.get("/api/config")
def public_config() -> dict:
    backends = resolve_backends(settings)
    return {
        "version": settings.app_version,
        "analytics": analytics_config(settings),
        "support_email": settings.support_email,
        "sentry": sentry_enabled(),
        "platforms": ["lichess"] + (["chesscom"] if settings.chesscom_enabled else []),
        "coach_mode": "agent" if backends else "offline",
        "coach_models": [b.label for b in backends],
        "email_configured": mailer.configured,
        "require_verified_email": settings.require_verified_email,
        "preview_games": settings.preview_games,
        "event_pass_days": EVENT_PASS_DAYS,
    }


@app.get("/api/health")
def health() -> dict:
    """Small and fast: for uptime monitors. /api/status has the full picture."""
    backends = resolve_backends(settings)
    return {
        "ok": True,
        "engine": state["engine"] is not None,
        "engine_error": state["engine_error"],
        "coach_mode": "agent" if backends else "offline",
        "coach_model": backends[0].label if backends else None,
        "demo_available": DEMO_PGN.exists(),
        "autosync": settings.autosync_enabled,
        "platforms": ["lichess"] + (["chesscom"] if settings.chesscom_enabled else []),
        "plans": list(PLANS),
    }


@app.get("/api/status")
def status(external: bool = True) -> dict:
    return status_checker.report(include_external=external)


def _small_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<title>{title}</title><link rel=stylesheet href='/static/app.css'>"
                        f"<body><main class='wrap' style='max-width:34rem;padding-top:4rem'><p class='brand'>♜ Plateau Breaker</p>"
                        f"<h1 style='margin:18px 0 12px'>{title}</h1>{body}</main></body>")


@app.get("/api/digest/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(token: str = "") -> HTMLResponse:
    """A GET only shows a button: email link-scanners open links, and must not unsubscribe people."""
    if not store.user_by_unsubscribe_token(token):
        return _small_page("Link not valid", "<p>Sign in and change your email settings under Account.</p><p><a href='/'>Back to the app</a></p>")
    tok = html_escape(token, quote=True)
    return _small_page("Stop the weekly email?",
                       f"<form method=post action='/api/digest/unsubscribe?token={tok}'><button class=btn type=submit>Unsubscribe</button></form>"
                       "<p class='small muted' style='margin-top:12px'>You can turn it back on any time under Account.</p>")


@app.post("/api/digest/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str = "") -> HTMLResponse:
    """Also the RFC 8058 one-click endpoint mail apps call from the List-Unsubscribe header."""
    user = store.user_by_unsubscribe_token(token)
    if user:
        store.update_user(user["id"], digest_opt_in=0)
    msg = "You won't get the weekly email any more. You can turn it back on under Account." if user \
        else "That unsubscribe link isn't valid. Sign in and change it under Account."
    return _small_page("Unsubscribed" if user else "Link not valid", f"<p>{msg}</p><p><a href='/'>Back to the app</a></p>")


# ---- pages ----------------------------------------------------------------------------------------
@app.get("/status")
def status_page() -> FileResponse:
    return FileResponse(STATIC / "status.html")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
@app.get("/join")
@app.get("/verify")
@app.get("/reset")
@app.get("/app")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
