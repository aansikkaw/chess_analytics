"""Test settings: tiny engine budget, a throwaway database, no real LLM or network.

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
os.environ["ENABLE_AUTOSYNC"] = "0"
os.environ["SIGNUPS_PER_HOUR"] = "100000"  # the suite signs up many users from one address
os.environ["JOB_MODE"] = "inline"
os.environ.setdefault("TWIN_GAMES", "12")  # Opponent Twin builds study fewer games, at low depth, in tests
os.environ.setdefault("TWIN_DEPTH", "4")  # jobs run to completion inside the request, so tests see results at once
os.environ.setdefault("BANK_DEPTH", "4")  # predicted puzzles: shallow annotation of a small pool
os.environ.setdefault("PREDICT_POOL", "24")
os.environ.setdefault("SELF_TWIN_MIN_GAMES", "2")  # tests import 3 demo games; your Twin needs 10 in real use
os.environ.setdefault("THINK_DEPTH", "4")
os.environ.setdefault("REVIEW_DEPTH", "6")  # game report cards: shallow in tests
for _k in ("VISION_PROVIDER", "VISION_MODEL", "VISION_BASE_URL", "VISION_API_KEY", "VISION2_MODEL", "VISION2_BASE_URL", "VISION2_API_KEY",
           "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
    os.environ.pop(_k, None)  # photo reading is mocked in tests, never a real model
os.environ.pop("SENTRY_DSN", None)
# Never send real email from the test suite, even inside a codespace that has email secrets set.
for _k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PORT", "SMTP_SECURITY", "SMTP_FROM", "EMAIL_FROM",
           "BREVO_API_KEY", "RESEND_API_KEY", "SUPPORT_EMAIL", "PUBLIC_URL", "CODESPACE_NAME"):
    os.environ.pop(_k, None)
os.environ.pop("BACKUP_TARGET", None)
os.environ["ADMIN_EMAILS"] = "admin@test.com"
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests never call a real LLM
os.environ.setdefault("COACH_PROVIDER", "offline")

from app.config import load_settings  # noqa: E402

SETTINGS = load_settings()
needs_engine = pytest.mark.skipif(not SETTINGS.stockfish_path, reason="Stockfish not installed")


@pytest.fixture(scope="session")
def engine():
    if not SETTINGS.stockfish_path:
        pytest.skip("Stockfish not installed")
    from app.engine import Engine

    with Engine(SETTINGS.stockfish_path, depth=8, time_limit=0.05) as e:
        yield e


@pytest.fixture(scope="session")
def demo_games(engine):
    """Six analysed demo games, shared by the retrieval, coach and Pro-feature tests."""
    from pathlib import Path

    from app.analysis import analyse_games
    from app.games import parse_pgn

    pgn = (Path(__file__).resolve().parent.parent / "sample_data" / "demo_games.pgn").read_text()
    return analyse_games(parse_pgn(pgn, "demo_player")[:6], engine)


@pytest.fixture(scope="session")
def demo_bundle(demo_games, tmp_path_factory):
    """The demo games written as an OKF bundle, with the principles bundle mounted at /knowledge."""
    from app.okf import Bundle
    from app.player_bundle import build_player_bundle
    from app.sync import KNOWLEDGE_DIR

    out = build_player_bundle(tmp_path_factory.mktemp("okf") / "player", demo_games, "demo_player", "demo")
    return Bundle.load(out).mount(KNOWLEDGE_DIR, "/knowledge")
