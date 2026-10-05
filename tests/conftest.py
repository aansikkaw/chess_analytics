"""Test settings: tiny engine budget and a throwaway database.

These env vars must be set before `app.main` is imported, because it reads
settings at import time.
"""

import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="plateau-test-")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("ENGINE_DEPTH", "6")
os.environ.setdefault("ENGINE_TIME", "0.02")
os.environ.setdefault("MAX_GAMES", "3")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests never call a real LLM
os.environ.setdefault("COACH_PROVIDER", "offline")

from app.config import load_settings  # noqa: E402

SETTINGS = load_settings()
needs_engine = pytest.mark.skipif(not SETTINGS.stockfish_path, reason="Stockfish not installed")
