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


def load_settings() -> Settings:
    return Settings(
        stockfish_path=_find_stockfish(),
        engine_depth=int(os.getenv("ENGINE_DEPTH", "12")),
        engine_time=float(os.getenv("ENGINE_TIME", "0.08")),
        max_games=int(os.getenv("MAX_GAMES", "60")),
        db_path=os.getenv("DB_PATH", str(PROJECT_ROOT / "plateau.db")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        coach_model=os.getenv("COACH_MODEL", "claude-sonnet-5-5"),
        coach_provider=os.getenv("COACH_PROVIDER", "auto").strip().lower(),
        llm_base_url=(os.getenv("LLM_BASE_URL") or "").rstrip("/") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        ollama_host=os.getenv("OLLAMA_HOST_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
    )
