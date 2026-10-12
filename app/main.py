"""HTTP API + static front end.

Run:  uvicorn app.main:app --reload

Every player route checks that the linked chess account belongs to the signed-in
user, and every Pro route checks the plan. Data never crosses accounts.
Engine-heavy work (imports, Prep Checks, previews) goes through the job queue (app/jobs.py).
"""

from __future__ import annotations

import io
import json
import logging
import os
from html import escape as html_escape
import time
import uuid
import zipfile
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path

import chess
import chess.engine
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .analysis import classify, move_accuracy, win_percent
from .coach import Coach, CoachTools
from .config import load_settings
from .engine import Engine, EngineUnavailable
from .games import TIME_CLASSES, GameImportError, decode_upload, name_matches, parse_pgn, player_names, validate_pgn_name, validate_username
from .jobs import JobQueue
from . import twin as twin_mod
from .library import ALL_CLASSES, Library, board_from, to_pgn
from .llm import resolve_backends
from .mailer import (MailError, Mailer, coach_invite_email, plan_request_admin_email, plan_request_email, reset_email, support_email,
                     verification_email)
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
from . import legal, switches
from .status import CoachStats, StatusChecker
from .store import Store
from .sync import DEMO_HANDLE, DEMO_PGN, KNOWLEDGE_DIR, AutoSync, Importer, Jobs, player_key

if not logging.getLogger().handlers:  # uvicorn configures only its own loggers; give ours a home too
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
library = Library(store)
coach_stats = CoachStats()
login_limiter = auth.SharedLimiter(store, "login")
preview_limiter = auth.SharedLimiter(store, "preview", limit=settings.previews_per_hour, window=3600)
forgot_limiter = auth.SharedLimiter(store, "forgot", limit=5, window=3600)
support_limiter = auth.SharedLimiter(store, "support", limit=5, window=3600)
client_error_limiter = auth.SharedLimiter(store, "client_error", limit=30, window=600)
resend_limiter = auth.SharedLimiter(store, "resend", limit=3, window=3600)
analyse_limiter = auth.SharedLimiter(store, "analyse", limit=240, window=3600)  # engine look-ups from Explore, per user
signup_limiter = auth.SharedLimiter(store, "signup", limit=settings.signups_per_hour, window=3600)
test_email_limiter = auth.SharedLimiter(store, "test_email", limit=10, window=3600)
export_limiter = auth.SharedLimiter(store, "export", limit=5, window=3600)
twin_move_limiter = auth.SharedLimiter(store, "twin_move", limit=1500, window=3600)  # Twin moves per user per hour
twin_review_limiter = auth.SharedLimiter(store, "twin_review", limit=40, window=3600)


def audit(user: dict | None, action: str, target: str | None = None, **detail) -> None:
    """Record who did what (admin, roster, account and data actions). Never lets a logging problem break the request."""
    try:
        store.audit(user["email"] if user else "system", action, target, detail or None, user["id"] if user else None)
    except Exception:  # noqa: BLE001
        log.exception("audit write failed: %s %s", action, target)
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


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
access_log = logging.getLogger("plateau.access")


def _same_origin(origin: str, request: Request) -> bool:
    """Is a browser's Origin header this site? Matches PUBLIC_URL, or the host the request came in on."""
    o = urlparse(origin)
    if not o.scheme or not o.netloc:
        return False
    if origin.rstrip("/") == settings.public_url:
        return True
    hosts = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")} - {""}
    return o.netloc in hosts


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """CSRF defence on top of SameSite cookies: a browser on another site can't make us change anything.
    Browsers always send Origin (and Sec-Fetch-Site) on cross-site writes; tools like curl send neither."""
    if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != "null" and not _same_origin(origin, request)):
            return JSONResponse({"detail": "Requests from other websites aren't allowed."}, status_code=403)
    return await call_next(request)


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


@app.middleware("http")
async def request_ids(request: Request, call_next):
    """Every response carries an X-Request-ID, and every request gets one log line (JSON) with the same ID,
    so a support email quoting the ID leads straight to the log line and the Sentry event."""
    rid = (request.headers.get("x-request-id") or "")[:64] or uuid.uuid4().hex[:12]
    request.state.rid = rid
    start = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:  # noqa: BLE001 - an unhandled error: answer with the reference instead of a bare 500
        access_log.exception("request failed rid=%s", rid)
        report_exception(rid)
        resp = JSONResponse({"detail": f"Something went wrong on our side. If it keeps happening, contact support and quote {rid}.",
                             "request_id": rid}, status_code=500)
    resp.headers["X-Request-ID"] = rid
    path = request.url.path  # never the query string: it can hold one-time tokens
    if not path.startswith(("/static/", "/api/health")):
        access_log.info(json.dumps({"rid": rid, "method": request.method, "path": path, "status": resp.status_code,
                                    "ms": round((time.perf_counter() - start) * 1000, 1)}))
    return resp


def report_exception(rid: str) -> None:
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", rid)
        sentry_sdk.capture_exception()
    except ImportError:
        pass


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
    if platform == "chesscom" and not switches.is_on(store, "chesscom"):
        raise HTTPException(503, switches.PAUSED_MESSAGES["chesscom"])


def imports_allowed() -> None:
    if not switches.is_on(store, "imports"):
        raise HTTPException(503, switches.PAUSED_MESSAGES["imports"])


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
    accept_terms: bool = False  # sign-up only: "I'm 18 or over and agree to the Terms and Privacy Policy"


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
    if not switches.is_on(store, "signups"):
        raise HTTPException(503, switches.PAUSED_MESSAGES["signups"])
    if signup_limiter.blocked(ip):
        raise HTTPException(429, "Too many new accounts from this network. Try again in an hour.")
    try:
        email = auth.normalize_email(body.email)
        auth.check_password_strength(body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    if settings.require_terms and not body.accept_terms:
        raise HTTPException(400, "Please confirm you're 18 or over and agree to the Terms and Privacy Policy.")
    signup_limiter.fail(ip)
    is_admin = email in admin_emails()
    try:
        uid = store.create_user(email, auth.hash_password(body.password), "pro" if is_admin else "free", is_admin)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if body.accept_terms:
        store.update_user(uid, terms_accepted_at=time.time())
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
        "limits": {k: p.get(k, 0) for k in ("max_games_per_sync", "max_accounts", "coach_messages_per_day", "scouts_per_day", "library_players")},
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
    store.audit(f"user:{user['id']}", "account_deleted", None, None, user["id"])  # no email: the account is gone
    for acct in store.delete_user(user["id"]):
        importer.knowledge.remove(acct)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me/export")
def export_me(user: dict = Depends(current_user)) -> StreamingResponse:
    """Everything we hold about you, as a zip of JSON files (your right of access under DPDP and GDPR)."""
    key = f"user:{user['id']}"
    if export_limiter.blocked(key):
        raise HTTPException(429, "You've downloaded your data a few times already. Try again in an hour.")
    export_limiter.fail(key)
    audit(user, "data_exported")
    data = store.export_user(user["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "Your Plateau Breaker data, exported " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + ".\n\n"
                   "account.json        your account, linked chess accounts and settings (times are Unix seconds)\n"
                   "games/*.json        every analysed game, per linked account\n"
                   "puzzles/*.json      puzzles made from your mistakes, with your training progress\n"
                   "activity.json       repertoires, daily usage, plan requests, support messages, coaching, security log\n\n"
                   "Questions: " + (settings.support_email or settings.public_url) + "\n")
        dump = lambda obj: json.dumps(obj, indent=2, default=str)  # noqa: E731
        z.writestr("account.json", dump({"user": data["user"], "accounts": data["accounts"]}))
        for label, games in data["games"].items():
            z.writestr(f"games/{label}.json", dump(games))
        for label, rows in data["puzzles"].items():
            z.writestr(f"puzzles/{label}.json", dump(rows))
        z.writestr("activity.json", dump({k: data[k] for k in ("repertoires", "usage", "plan_requests", "support_messages", "roster", "opponent_library",
                                                                "homework_set", "squads_joined", "homework_received", "security_log")}))
    buf.seek(0)
    fname = f"plateau-breaker-data-{time.strftime('%Y%m%d')}.zip"
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/plans")
def plans() -> list[dict]:
    return public_plans()


@app.post("/api/upgrade-request")
def upgrade_request(body: UpgradeRequest, background: BackgroundTasks, user: dict = Depends(current_user)) -> dict:
    if body.plan not in OFFER_IDS or body.plan == "free":
        raise HTTPException(400, "Unknown plan.")
    store.add_upgrade_request(user["id"], body.note, body.plan)
    audit(user, "plan_requested", body.plan)
    name = {"coach": "the Coach plan", "event": f"a {EVENT_PASS_DAYS}-day Event Pass"}.get(body.plan, "Pro")
    command = f"python scripts/admin.py grant {user['email']}" + {"coach": " --plan coach", "event": f" --days {EVENT_PASS_DAYS}"}.get(body.plan, "")
    subject, text, html = plan_request_email(name)
    background.add_task(mailer.send, user["email"], subject, text, html)
    for owner in sorted({settings.support_email, *admin_emails()} - {None, ""}):  # tell the owner, with the command to run
        subject, text = plan_request_admin_email(user["email"], name, command, body.note)
        background.add_task(mailer.send, owner, subject, text, None, user["email"])
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
    imports_allowed()
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
    notice = None
    if backends and not switches.is_on(store, "ai_coach"):
        backends, notice = [], switches.PAUSED_MESSAGES["ai_coach"]
    budget = plan_of(user).get("ai_tokens_per_day", 0)
    if backends and budget and store.usage_today(user["id"], "ai_tokens") >= budget:
        backends, notice = [], "You've used today's AI coach allowance, so the built-in coach is answering. It resets at midnight UTC."
    r = Coach(acct["handle"], tools, backends).chat(req.message, req.history)  # never raises: falls back to offline
    if r.tokens:
        store.bump_usage(user["id"], "ai_tokens", r.tokens)
    if notice and not r.notice:
        r.notice = notice
    if not notice:  # a deliberate pause or cap isn't a provider failure
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
    """Scout a player: download (or update) their games into your Opponent Library, then build the report.
    Only downloads count towards the daily limit; a player fetched in the last 30 minutes opens from the library."""
    require_feature(user, "scout")
    player, downloaded, new = _library_download(user, req.platform, req.handle, req.max_games)
    library.save(user["id"], player["id"])
    return _scout_from_library(user, player["id"], req.max_games, req.account_id) | {"downloaded": downloaded, "new_games": new}


# ---- Opponent Library (a ChessBase-style database of the players you prepare against) --------------------
def library_user(user: dict = Depends(verified_user)) -> dict:
    require_feature(user, "scout")
    return user


def _library_room(user: dict, player_id: int | None) -> None:
    cap = plan_of(user).get("library_players", 0)
    if (player_id is None or not library.has(user["id"], player_id)) and library.count(user["id"]) >= cap:
        raise HTTPException(402, {"message": f"Your library holds {cap} players. Remove one to add another.", "upgrade": False})


def _library_download(user: dict, platform: str, handle: str, want: int) -> tuple[dict, bool, int]:
    """Make sure the player is in the library with up to `want` games, downloading only what's missing."""
    platform_allowed(platform)
    imports_allowed()
    limit = plan_of(user)["scouts_per_day"]
    try:
        handle = check_account(platform, handle)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    player = library.get_or_create_player(platform, handle)
    _library_room(user, player["id"])
    want = min(want, games_cap(user))
    if not library.needs_download(player, want):
        return player, False, 0
    if store.usage_today(user["id"], "scout") >= limit:
        if player["games"]:  # out of downloads today, but we still have their games: use those
            return player, False, 0
        spend(user, "scout", limit)  # raises the usual "daily limit reached" error
    since = library.fetch_plan(player, want)
    try:
        recs = fetch_games(platform, handle, want, ALL_CLASSES, **({"since_ms": since} if since else {}))
    except GameImportError as exc:
        if player["games"]:  # the site is down: carry on with what's stored
            return player, False, 0
        raise HTTPException(400, str(exc)) from exc
    new = library.add_games(player["id"], recs, requested=want if since is None else None)
    spend(user, "scout", limit)  # only successful downloads count towards the daily limit
    return library.player(player["id"]), True, new


def _owned_library_player(user: dict, player_id: int) -> dict:
    if not library.has(user["id"], player_id):
        raise HTTPException(404, "That player isn't in your library.")
    return library.player(player_id)


def _scout_from_library(user: dict, player_id: int, games: int, account_id: int | None) -> dict:
    player = library.player(player_id)
    recs = library.records(player_id, limit=max(10, min(games, 1000)))
    my_games = store.load_games(player_key(owned_account(account_id, user))) if account_id is not None else None
    report = scout_report(player["handle"], recs, state["engine"], my_games)
    return report | {"player": library.entry(user["id"], player_id)}


class LibraryAdd(BaseModel):
    platform: str = Field(..., pattern="^(lichess|chesscom)$")
    handle: str = Field(..., max_length=30)
    max_games: int = Field(200, ge=10, le=500)


class LibraryFilters(BaseModel):
    players: list[int] = Field(default_factory=list, max_length=500)
    color: str | None = Field(None, pattern="^(white|black)$")
    result: str | None = Field(None, pattern="^(win|draw|loss)$")
    time_classes: list[str] | None = Field(None, max_length=4)
    opening: str | None = Field(None, max_length=80)
    opponent: str | None = Field(None, max_length=60)
    date_from: str | None = Field(None, pattern=r"^\d{4}[-.]\d{2}[-.]\d{2}$")
    date_to: str | None = Field(None, pattern=r"^\d{4}[-.]\d{2}[-.]\d{2}$")


class LibraryGamesRequest(LibraryFilters):
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


class ExploreRequest(LibraryFilters):
    moves: list[str] = Field(default_factory=list, max_length=300)
    fen: str | None = Field(None, max_length=100)


class LibraryPatch(BaseModel):
    notes: str | None = Field(None, max_length=4000)
    tags: list[str] | None = Field(None, max_length=12)


def _filters(body: LibraryFilters) -> dict:
    return body.model_dump(exclude={"players", "limit", "offset", "moves", "fen"})


@app.get("/api/library")
def library_list(user: dict = Depends(library_user)) -> dict:
    rows = library.entries(user["id"])
    return {"players": rows, "limit": plan_of(user).get("library_players", 0), "tags": sorted({t for r in rows for t in r["tags"]}),
            "downloads_left": max(0, plan_of(user)["scouts_per_day"] - store.usage_today(user["id"], "scout"))}


@app.post("/api/library")
def library_add(body: LibraryAdd, user: dict = Depends(library_user)) -> dict:
    player, downloaded, new = _library_download(user, body.platform, body.handle, body.max_games)
    library.save(user["id"], player["id"])
    return {"player": library.entry(user["id"], player["id"]), "downloaded": downloaded, "new_games": new}


@app.post("/api/library/{player_id}/update")
def library_update(player_id: int, user: dict = Depends(library_user)) -> dict:
    p = _owned_library_player(user, player_id)
    if p["platform"] == "pgn":
        raise HTTPException(400, "This player's games come from a PGN upload. Upload a newer file to add games.")
    _, downloaded, new = _library_download(user, p["platform"], p["handle"], max(p.get("requested") or 0, p["games"], 10))
    msg = (f"{new} new game{'s' if new != 1 else ''} added." if downloaded else "Already up to date.")
    return {"player": library.entry(user["id"], player_id), "downloaded": downloaded, "new_games": new, "message": msg}


@app.get("/api/library/{player_id}/report")
def library_report(player_id: int, games: int = 200, account_id: int | None = None, user: dict = Depends(library_user)) -> dict:
    _owned_library_player(user, player_id)
    library.save(user["id"], player_id)  # marks it as recently opened
    return _scout_from_library(user, player_id, games, account_id)


@app.patch("/api/library/{player_id}")
def library_patch(player_id: int, body: LibraryPatch, user: dict = Depends(library_user)) -> dict:
    _owned_library_player(user, player_id)
    library.update_entry(user["id"], player_id, body.notes, body.tags)
    return {"player": library.entry(user["id"], player_id)}


@app.delete("/api/library/{player_id}")
def library_remove(player_id: int, user: dict = Depends(library_user)) -> dict:
    if not library.remove(user["id"], player_id):
        raise HTTPException(404, "That player isn't in your library.")
    return {"ok": True}


@app.post("/api/library/games")
def library_games(body: LibraryGamesRequest, user: dict = Depends(library_user)) -> dict:
    return library.games(library.owned_ids(user["id"], body.players), _filters(body), body.limit, body.offset)


@app.post("/api/library/explore")
def library_explore(body: ExploreRequest, user: dict = Depends(library_user)) -> dict:
    try:
        board = board_from(body.moves, body.fen)
    except ValueError as exc:
        raise HTTPException(400, str(exc) if "FEN" in str(exc) else "Those moves aren't legal from that position.") from exc
    return library.explore(library.owned_ids(user["id"], body.players), board, _filters(body))


@app.get("/api/library/games/{game_ref}")
def library_game(game_ref: int, user: dict = Depends(library_user)) -> dict:
    """One library game for the board viewer (same shape as an analysed game, without engine marks)."""
    found = library.game(game_ref)
    if not found or not library.has(user["id"], found[0]["player_id"]):
        raise HTTPException(404, "No such game.")
    row, rec = found
    board, fens, ucis, sans = chess.Board(), [chess.STARTING_FEN], [], []
    for uci in rec.moves:
        try:
            mv = chess.Move.from_uci(uci)
            sans.append(board.san(mv))
        except ValueError:
            break
        ucis.append(uci)
        board.push(mv)
        fens.append(board.fen())
    return {"game_id": rec.game_id, "title": f"{rec.white} vs {rec.black}", "library": True, "player": row["player"],
            "opponent": row["opponent"], "color": row["color"], "result": rec.result, "opening": rec.opening, "played_at": rec.played_at,
            "accuracy": None, "url": rec.url, "time_class": rec.time_class, "moves_san": sans, "ucis": ucis, "fens": fens, "marks": [],
            "pgn": to_pgn(rec)}


@app.post("/api/library/upload")
async def library_upload(file: UploadFile = File(...), name: str = Form(""), user: dict = Depends(library_user)) -> dict:
    """Add an over-the-board opponent from a ChessBase / PGN file. Private to you."""
    data = await file.read()
    try:
        text = decode_upload(data, file.filename or "")
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    name = name.strip()
    if not name:
        names = player_names(text, 12)
        if not names:
            raise HTTPException(400, "No games found in that file.")
        return {"choose": [{"name": n, "games": c} for n, c in names]}
    try:
        name = validate_pgn_name(name)
        recs = parse_pgn(text, name)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    player = library.get_or_create_player("pgn", name, owner_id=user["id"])
    _library_room(user, player["id"])
    new = library.add_games(player["id"], recs, requested=len(recs))
    library.save(user["id"], player["id"])
    return {"player": library.entry(user["id"], player["id"]), "new_games": new,
            "message": f"Added {new} game{'s' if new != 1 else ''} for {name}." + (" The rest were already in your library." if new < len(recs) else "")}


@app.get("/api/library/export.pgn")
def library_export(request: Request, user: dict = Depends(library_user)) -> Response:
    """The selected players' games (with the same filters as the Games list) as one PGN file for ChessBase."""
    q = request.query_params
    try:
        body = LibraryFilters(players=[int(x) for x in q.get("players", "").split(",") if x.strip()],
                              color=q.get("color") or None, result=q.get("result") or None,
                              time_classes=[t for t in q.get("time_classes", "").split(",") if t] or None,
                              opening=q.get("opening") or None, opponent=q.get("opponent") or None,
                              date_from=q.get("date_from") or None, date_to=q.get("date_to") or None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Those filters aren't valid.") from exc
    ids = library.owned_ids(user["id"], body.players)
    text = library.pgn(ids, _filters(body))
    names = [library.player(i)["handle"] for i in ids[:3]]
    fname = ("-".join(names) + ("-and-more" if len(ids) > 3 else "") or "library").replace(" ", "_")[:60]
    return Response(text, media_type="application/x-chess-pgn", headers={"Content-Disposition": f'attachment; filename="{fname}.pgn"'})


# ---- Opponent Twin: practise against a model of a specific opponent -----------------------------------
class TwinBuild(BaseModel):
    time_classes: list[str] | None = Field(None, max_length=4)


class TwinMove(BaseModel):
    moves: list[str] = Field(default_factory=list, max_length=600)
    time_classes: list[str] | None = Field(None, max_length=4)
    mode: str = Field("faithful", pattern="^(faithful|good_day)$")
    main_line: bool = False  # rehearse: always their most common move while in their book


class TwinReview(BaseModel):
    moves: list[str] = Field(..., min_length=1, max_length=600)
    color: str = Field(..., pattern="^(white|black)$")
    time_classes: list[str] | None = Field(None, max_length=4)


def _twin_allowed() -> None:
    if not switches.is_on(store, "twin"):
        raise HTTPException(503, switches.PAUSED_MESSAGES["twin"])


def _twin_classes(classes: list[str] | None) -> list[str]:
    return [c for c in (classes or []) if c in twin_mod.TIME_CLASSES]


def _twin_view(t: dict | None, player: dict, classes: list[str]) -> dict | None:
    if not t:
        return None
    with store._conn() as c:
        sql = "SELECT COUNT(*), MAX(played_at) FROM lib_games WHERE player_id = ?"
        args: list = [player["id"]]
        if classes:
            sql += f" AND time_class IN ({','.join('?' * len(classes))})"
            args += classes
        count, newest = c.execute(sql, args).fetchone()
    stale = bool(newest and newest > t["newest_game"]) and (count or 0) - t["games"] >= 0
    return {k: t[k] for k in ("tc_key", "built_at", "games", "decisions", "newest_game", "profile")} | {"stale": stale, "games_available": count}


@app.get("/api/twin/{player_id}")
def twin_status(player_id: int, tc: str = "", user: dict = Depends(library_user)) -> dict:
    player = _owned_library_player(user, player_id)
    classes = _twin_classes(tc.split(",") if tc else None)
    key = twin_mod.tc_key(classes)
    job = queue.find_recent("twin", f"{player_id}:{key}", 3600)
    active = job["id"] if job and job["state"] in ("queued", "running") and job["user_id"] == user["id"] else None
    with store._conn() as c:
        sql = "SELECT COUNT(*) FROM lib_games WHERE player_id = ?"
        args: list = [player_id]
        if classes:
            sql += f" AND time_class IN ({','.join('?' * len(classes))})"
            args += classes
        games = c.execute(sql, args).fetchone()[0]
    left = max(0, plan_of(user).get("twin_builds_per_day", 0) - store.usage_today(user["id"], "twin_build"))
    return {"player": library.entry(user["id"], player_id), "tc_key": key, "twin": _twin_view(twin_mod.load(store, player_id, key), player, classes),
            "job_id": active, "games": games, "min_games": twin_mod.MIN_GAMES, "builds_left": left,
            "engine": state["engine"] is not None}


@app.post("/api/twin/{player_id}/build")
def twin_build(player_id: int, body: TwinBuild, user: dict = Depends(library_user)) -> dict:
    _twin_allowed()
    player = _owned_library_player(user, player_id)
    classes = _twin_classes(body.time_classes)
    key = twin_mod.tc_key(classes)
    existing = _twin_view(twin_mod.load(store, player_id, key), player, classes)
    if existing and not existing["stale"] and time.time() - existing["built_at"] < 86_400:
        return {"job_id": None, "message": "This Twin is already up to date.", "twin": existing}
    job = queue.find_recent("twin", f"{player_id}:{key}", 3600)
    if job and job["state"] in ("queued", "running") and job["user_id"] == user["id"]:
        return {"job_id": job["id"], "message": "Already building."}
    spend(user, "twin_build", plan_of(user).get("twin_builds_per_day", 0))
    job_id = jobs.submit("twin", {"player_id": player_id, "time_classes": classes, "key": f"{player_id}:{key}"}, user["id"])
    audit(user, "twin_built", f"library:{player_id}", time_classes=key)
    return {"job_id": job_id, "message": f"Building {player['handle']}'s Twin. It studies their games with the engine: usually 1-2 minutes."}


def _twin_or_404(player_id: int, classes: list[str]) -> dict:
    t = twin_mod.load(store, player_id, twin_mod.tc_key(classes))
    if not t:
        raise HTTPException(404, "Build this Twin first.")
    return t


def _game_over(board: chess.Board) -> dict | None:
    outcome = board.outcome(claim_draw=True)
    if not outcome:
        return None
    reason = {chess.Termination.CHECKMATE: "checkmate", chess.Termination.STALEMATE: "stalemate",
              chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material", chess.Termination.THREEFOLD_REPETITION: "repetition",
              chess.Termination.FIFTY_MOVES: "the 50-move rule", chess.Termination.FIVEFOLD_REPETITION: "repetition",
              chess.Termination.SEVENTYFIVE_MOVES: "the 75-move rule"}.get(outcome.termination, "the rules")
    return {"result": outcome.result(), "reason": reason}


@app.post("/api/twin/{player_id}/move")
def twin_move(player_id: int, body: TwinMove, user: dict = Depends(library_user)) -> dict:
    """The Twin's reply to a game so far (moves from the start, UCI or SAN). Stateless: the browser keeps the game."""
    _twin_allowed()
    _owned_library_player(user, player_id)
    key = f"user:{user['id']}"
    if twin_move_limiter.blocked(key):
        raise HTTPException(429, "That's a lot of moves this hour. Take a short break and play again soon.")
    twin_move_limiter.fail(key)
    engine = state["engine"]
    if engine is None:
        raise HTTPException(503, "Stockfish isn't running on this server, so the Twin can't think right now.")
    classes = _twin_classes(body.time_classes)
    t = _twin_or_404(player_id, classes)
    try:
        board = board_from(body.moves)
    except ValueError as exc:
        raise HTTPException(400, "Those moves aren't legal.") from exc
    over = _game_over(board)
    if over:
        return {"over": over, "fen": board.fen()}
    ucis = [m.uci() for m in board.move_stack]
    prev_to = chess.Move.from_uci(ucis[-2]).to_square if len(ucis) >= 2 else None
    opp_to = chess.Move.from_uci(ucis[-1]).to_square if ucis else None
    pick = twin_mod.choose(store, t, board, engine, classes=classes, mode=body.mode, main_line=body.main_line, prev_to=prev_to, opp_to=opp_to,
                           depth=settings.twin_depth)
    board.push(chess.Move.from_uci(pick["uci"]))
    return pick | {"fen": board.fen(), "over": _game_over(board)}


@app.post("/api/twin/{player_id}/review")
def twin_review(player_id: int, body: TwinReview, user: dict = Depends(library_user)) -> dict:
    """After a practice game: your accuracy and mistakes, the Twin's accuracy, and where the game left their book."""
    _owned_library_player(user, player_id)
    key = f"user:{user['id']}"
    if twin_review_limiter.blocked(key):
        raise HTTPException(429, "That's a lot of reviews this hour. Try again later.")
    twin_review_limiter.fail(key)
    engine = state["engine"]
    if engine is None:
        raise HTTPException(503, "Stockfish isn't running on this server, so games can't be reviewed right now.")
    try:
        board = board_from(body.moves)
    except ValueError as exc:
        raise HTTPException(400, "Those moves aren't legal.") from exc
    moves = list(board.move_stack)[:240]
    you = chess.WHITE if body.color == "white" else chess.BLACK
    classes = _twin_classes(body.time_classes)
    limit = chess.engine.Limit(depth=10, time=0.05)
    b = chess.Board()
    evals = [engine.evaluate(b, limit)]
    for mv in moves:
        b.push(mv)
        evals.append(engine.evaluate(b, limit))
    b = chess.Board()
    yours, theirs, mistakes = [], [], []
    left_book = None
    for ply, mv in enumerate(moves):
        mover = b.turn
        before = win_percent(evals[ply].cp_for(mover))
        after = win_percent(evals[ply + 1].cp_for(mover))
        acc = move_accuracy(before, after)
        if mover == you:
            yours.append(acc)
            label = classify(before - after)
            if label in ("inaccuracy", "mistake", "blunder"):
                best = evals[ply].best_move
                mistakes.append({"ply": ply, "move": f"{ply // 2 + 1}{'.' if mover == chess.WHITE else '...'}{b.san(mv)}",
                                 "classification": label, "win_loss": round(before - after, 1), "fen": b.fen(), "played_uci": mv.uci(),
                                 "best_uci": best, "best": b.san(chess.Move.from_uci(best)) if best else None})
        else:
            theirs.append(acc)
            if left_book is None and not twin_mod.book(store, player_id, b, classes):
                line = chess.Board().variation_san(moves[:ply]).replace(". ", ".") if ply else ""
                if ply > 10:
                    line = "… " + " ".join(line.split()[-7:])
                left_book = {"move": ply // 2 + 1, "after": line}
        b.push(mv)
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    mistakes.sort(key=lambda m: -m["win_loss"])
    return {"your_accuracy": mean(yours), "twin_accuracy": mean(theirs), "mistakes": mistakes[:6],
            "left_book": left_book, "moves": len(moves), "over": _game_over(board)}


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
    consent: bool = False  # the student (or a parent or guardian, if under 18) agreed to their games being analysed here


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
    if not body.consent:
        raise HTTPException(400, "Please confirm the student (or their parent or guardian, if they're under 18) has agreed.")
    if body.platform != "pgn":
        platform_allowed(body.platform)
    try:
        handle = validate_pgn_name(body.handle) if body.platform == "pgn" else check_account(body.platform, body.handle)
    except GameImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    if any(a["platform"] == body.platform and a["handle"].lower() == handle.lower() for a in store.accounts(user["id"])):
        raise HTTPException(409, f"{handle} is already in your accounts or on your roster.")
    if body.platform != "pgn":
        imports_allowed()
    acct = store.add_account(user["id"], body.platform, handle, role="student")
    sid = store.add_student(user["id"], acct["id"], body.name.strip(), consent=True)
    audit(user, "student_added", f"student:{sid}", platform=body.platform, handle=handle)
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
    audit(user, "student_removed", f"student:{student_id}", name=st["name"])
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
    imports_allowed()
    queued = 0
    for st in store.students(user["id"]):
        acct = store.account(st["account_id"])
        live = acct and acct["platform"] in ("lichess", "chesscom") and settings_allows(acct["platform"])
        if live and jobs.submit("import", {"max_games": games_cap(user), "time_classes": acct["time_classes"].split(",")},
                                user["id"], acct["id"], exclusive=True):
            queued += 1
    return {"queued": queued, "message": f"Syncing {queued} student{'s' if queued != 1 else ''}. Each one updates as it finishes."}


def settings_allows(platform: str) -> bool:
    return platform != "chesscom" or (settings.chesscom_enabled and switches.is_on(store, "chesscom"))


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
    audit(user, "student_invited", f"student:{student_id}", emailed=bool(sent_to))
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
    audit(user, "squad_joined", f"student:{st['id']}", coach_id=st["coach_id"])
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
    audit(user, "squad_left", f"student:{student_id}")
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
    audit(user, "test_email")
    try:
        info = mailer.test(user["email"])
    except MailError as exc:
        return {"ok": False, "error": str(exc), **mailer.describe()}
    msg = (f"Sent a test email to {user['email']}. It usually arrives within a minute; check spam too."
           if mailer.configured else "Email isn't set up, so the test was saved to the outbox/ folder instead.")
    return {"ok": True, "message": msg, **info}


class SwitchBody(BaseModel):
    on: bool


@app.get("/api/admin/switches")
def admin_switches(user: dict = Depends(admin_user)) -> list[dict]:
    """Kill switches: pause one part of the service without a redeploy."""
    return [{"name": n, "label": label, "on": switches.is_on(store, n)} for n, label in switches.SWITCHES.items()]


@app.post("/api/admin/switches/{name}")
def admin_set_switch(name: str, body: SwitchBody, user: dict = Depends(admin_user)) -> dict:
    if name not in switches.SWITCHES:
        raise HTTPException(404, "No such switch.")
    switches.set_switch(store, name, body.on, actor=user["email"])
    return {"ok": True, "name": name, "on": body.on}


@app.get("/api/admin/audit")
def admin_audit(limit: int = 100, user: dict = Depends(admin_user)) -> list[dict]:
    """The most recent entries in the audit log: admin, roster, account and data actions."""
    return store.audit_entries(max(1, min(limit, 500)))


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
        "require_terms": settings.require_terms,
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
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
@app.get("/refunds", response_class=HTMLResponse)
def legal_page(request: Request) -> HTMLResponse:
    return HTMLResponse(legal.render(request.url.path.strip("/"), settings))


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
