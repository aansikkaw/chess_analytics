"""Runtime settings, read once from environment variables.

Every setting has a safe default so the app runs locally with no configuration
beyond having Stockfish installed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_stockfish() -> str | None:
    """Look for Stockfish: env var first, then a bundled ./bin copy, then PATH."""
    explicit = os.getenv("STOCKFISH_PATH")
    if explicit:
        return explicit
    bundled = PROJECT_ROOT / "bin" / "stockfish"
    if bundled.exists():
        return str(bundled)
    # Debian/Ubuntu's apt package installs to /usr/games, which isn't always on PATH.
    return shutil.which("stockfish") or shutil.which("stockfish", path="/usr/games")


@dataclass(frozen=True)
class Settings:
    stockfish_path: str | None
    engine_depth: int
    engine_time: float  # seconds per position; whichever of depth/time hits first wins
    max_games: int
    db_path: str
    anthropic_api_key: str | None
    coach_model: str  # Claude model, used when the provider is "anthropic"
    coach_provider: str  # auto | anthropic | openai | ollama | offline
    llm_base_url: str | None  # any OpenAI-compatible /v1 endpoint (Groq, Gemini, OpenRouter, ...)
    llm_api_key: str | None
    llm_model: str | None
    ollama_host: str
    ollama_model: str
    autosync_enabled: bool
    autosync_interval_min: float
    cookie_secure: bool
    max_concurrent_imports: int
    chesscom_enabled: bool  # commercial use of Chess.com data needs their approval; switch off until you have it
    # --- v0.5: queue, previews, email, monitoring (all optional) ---
    job_mode: str = "thread"  # thread (workers inside the web process) | external (python -m app.worker) | inline (tests)
    import_chunk: int = 20  # games analysed per turn before a long import goes to the back of the queue
    preview_games: int = 15
    preview_depth: int = 8
    preview_time: float = 0.03
    twin_games: int = 150  # games an Opponent Twin studies per build ("Keep learning" studies the next batch)
    twin_depth: int = 9  # engine depth for the Twin's candidate moves (when building and when playing)
    library_full_max: int = 10_000  # server-wide cap on games kept per library player (full-history downloads)
    previews_per_hour: int = 6  # per IP address
    signups_per_hour: int = 10  # per IP address
    public_url: str = "http://localhost:8000"  # used in email links; detected automatically in Codespaces
    support_email: str | None = None
    brevo_api_key: str | None = None  # send through Brevo's HTTPS API (works where hosts block mail ports)
    resend_api_key: str | None = None  # or Resend's HTTPS API
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None  # EMAIL_FROM (or SMTP_FROM): the address emails come from
    smtp_security: str = "starttls"  # starttls | ssl | none (defaults to ssl on port 465)
    require_verified_email: bool = False  # if on, unverified users can't sync or ask the coach
    require_terms: bool = True  # new accounts must tick "I'm 18+ and agree to the Terms and Privacy Policy"
    llm_fallback_base_url: str | None = None  # a second OpenAI-compatible provider, tried when the first fails
    llm_fallback_api_key: str | None = None
    llm_fallback_model: str | None = None
    sentry_dsn: str | None = None
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = 0.0
    analytics: str = "none"  # none | plausible | posthog
    plausible_domain: str | None = None
    plausible_src: str = "https://plausible.io/js/script.tagged-events.js"
    posthog_key: str | None = None
    posthog_host: str = "https://eu.i.posthog.com"
    # --- v1.1: predicted puzzles, the "Think here" trainer, the scoresheet scanner ---
    bank_depth: int = 10  # engine depth when annotating bank puzzles for Twin predictions (once per puzzle, shared)
    think_depth: int = 10  # engine depth for finding critical moments in your games
    predict_pool: int = 300  # annotated bank puzzles to keep ready around each player's level
    self_twin_min_games: int = 10  # games your own Twin needs before it's used for predictions
    vision_provider: str = "auto"  # auto | anthropic | openai | off: which model reads scoresheet photos
    vision_model: str | None = None
    vision_base_url: str | None = None
    vision_api_key: str | None = None
    scans_per_hour: int = 20  # photo scans per user per hour (on top of the plan's daily allowance)
    review_depth: int = 14  # engine depth for full game reviews (both sides, top two moves per position)
    vision2_model: str | None = None  # a second photo reader (any OpenAI-compatible vision model); readings are merged by vote
    vision2_base_url: str | None = None
    vision2_api_key: str | None = None
    app_version: str = "1.2.2"


def _public_url() -> str:
    """Where people open the app, for links in emails.

    PUBLIC_URL wins. In GitHub Codespaces it's worked out from the forwarded-port address,
    so verification and reset links open your codespace instead of localhost.
    """
    explicit = (os.getenv("PUBLIC_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    name, domain = os.getenv("CODESPACE_NAME"), os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if name and domain:
        return f"https://{name}-{os.getenv('PORT') or '8000'}.{domain}"
    return "http://localhost:8000"


def load_settings() -> Settings:
    smtp_port = int(os.getenv("SMTP_PORT") or "587")
    return Settings(
        stockfish_path=_find_stockfish(),
        engine_depth=int(os.getenv("ENGINE_DEPTH", "12")),
        engine_time=float(os.getenv("ENGINE_TIME", "0.08")),
        max_games=int(os.getenv("MAX_GAMES", "500")),  # server-wide cap per sync, whatever the plan
        db_path=os.getenv("DB_PATH", str(PROJECT_ROOT / "plateau.db")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        coach_model=os.getenv("COACH_MODEL", "claude-sonnet-5-5"),
        coach_provider=os.getenv("COACH_PROVIDER", "auto").strip().lower(),
        llm_base_url=(os.getenv("LLM_BASE_URL") or "").rstrip("/") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        ollama_host=os.getenv("OLLAMA_HOST_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        autosync_enabled=os.getenv("ENABLE_AUTOSYNC", "1") == "1",
        autosync_interval_min=float(os.getenv("AUTOSYNC_INTERVAL_MIN", "360")),
        cookie_secure=os.getenv("COOKIE_SECURE", "0") == "1",
        max_concurrent_imports=int(os.getenv("MAX_CONCURRENT_IMPORTS", "2")),
        chesscom_enabled=os.getenv("CHESSCOM_ENABLED", "1") == "1",
        job_mode=os.getenv("JOB_MODE", "thread").strip().lower(),
        import_chunk=max(1, int(os.getenv("IMPORT_CHUNK", "20"))),
        preview_games=max(3, min(40, int(os.getenv("PREVIEW_GAMES", "15")))),
        preview_depth=int(os.getenv("PREVIEW_DEPTH", "8")),
        preview_time=float(os.getenv("PREVIEW_TIME", "0.03")),
        twin_games=max(10, int(os.getenv("TWIN_GAMES", "150"))),
        library_full_max=max(500, int(os.getenv("LIBRARY_FULL_MAX", "10000"))),
        twin_depth=max(3, int(os.getenv("TWIN_DEPTH", "9"))),
        previews_per_hour=int(os.getenv("PREVIEWS_PER_HOUR", "6")),
        signups_per_hour=int(os.getenv("SIGNUPS_PER_HOUR", "10")),
        public_url=_public_url(),
        support_email=(os.getenv("SUPPORT_EMAIL") or "").strip() or None,
        brevo_api_key=(os.getenv("BREVO_API_KEY") or "").strip() or None,
        resend_api_key=(os.getenv("RESEND_API_KEY") or "").strip() or None,
        smtp_host=(os.getenv("SMTP_HOST") or "").strip() or None,
        smtp_port=smtp_port,
        smtp_user=(os.getenv("SMTP_USER") or "").strip() or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        smtp_from=(os.getenv("EMAIL_FROM") or os.getenv("SMTP_FROM") or "").strip() or None,
        smtp_security=(os.getenv("SMTP_SECURITY") or ("ssl" if smtp_port == 465 else "starttls")).strip().lower(),
        require_verified_email=os.getenv("REQUIRE_EMAIL_VERIFICATION", "0") == "1",
        require_terms=os.getenv("REQUIRE_TERMS", "1") == "1",
        llm_fallback_base_url=(os.getenv("LLM_FALLBACK_BASE_URL") or "").rstrip("/") or None,
        llm_fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY") or None,
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL") or None,
        sentry_dsn=os.getenv("SENTRY_DSN") or None,
        sentry_environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        sentry_traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
        analytics=os.getenv("ANALYTICS", "none").strip().lower(),
        plausible_domain=os.getenv("PLAUSIBLE_DOMAIN") or None,
        plausible_src=os.getenv("PLAUSIBLE_SRC", "https://plausible.io/js/script.tagged-events.js"),
        posthog_key=os.getenv("POSTHOG_KEY") or None,
        posthog_host=os.getenv("POSTHOG_HOST", "https://eu.i.posthog.com").rstrip("/"),
        bank_depth=max(3, int(os.getenv("BANK_DEPTH", "10"))),
        think_depth=max(3, int(os.getenv("THINK_DEPTH", "10"))),
        predict_pool=max(10, int(os.getenv("PREDICT_POOL", "300"))),
        self_twin_min_games=max(1, int(os.getenv("SELF_TWIN_MIN_GAMES", "10"))),
        vision_provider=os.getenv("VISION_PROVIDER", "auto").strip().lower(),
        vision_model=(os.getenv("VISION_MODEL") or "").strip() or None,
        vision_base_url=(os.getenv("VISION_BASE_URL") or "").rstrip("/") or None,
        vision_api_key=os.getenv("VISION_API_KEY") or None,
        scans_per_hour=max(1, int(os.getenv("SCANS_PER_HOUR", "20"))),
        review_depth=max(4, int(os.getenv("REVIEW_DEPTH", "14"))),
        vision2_model=(os.getenv("VISION2_MODEL") or "").strip() or None,
        vision2_base_url=(os.getenv("VISION2_BASE_URL") or "").rstrip("/") or None,
        vision2_api_key=os.getenv("VISION2_API_KEY") or None,
    )
