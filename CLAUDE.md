# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Plateau Breaker: an AI chess coach (FastAPI + SQLite + Stockfish, vanilla-JS ES-module front end, no build step). README.md is the user-facing manual (features by version, every env var, a code map); DEPLOY.md and RUNBOOK.md cover production and incidents.

## Commands

```bash
pip install -r requirements-dev.txt          # Stockfish must also be installed (devcontainer does both)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

python -m pytest -q                          # full suite; engine tests skip if Stockfish is missing
python -m pytest tests/test_twin.py -q       # one file
python -m pytest tests/test_api.py::test_name -q
ruff check app scripts tests                 # lint (line length 165)
for f in app/static/js/*.js app/static/js/views/*.js app/static/sw.js; do node --check "$f"; done   # JS syntax check, as CI does (Node comes from the devcontainer's Node feature)

python scripts/admin.py ...                  # users, grant/revoke plans, kill switches, audit log
python -m app.worker                         # separate job worker (with JOB_MODE=external)
python -m app.scheduler [--once]             # backups, restore test, weekly digest
```

CI (`.github/workflows/ci.yml`) runs ruff, `node --check`, pytest with real Stockfish, `pip-audit` on `requirements.lock`, and a Docker build + `/api/health` smoke test.

## Architecture

- **`app/main.py`** is the whole HTTP API and serves `app/static/`. Settings are read at import time via `app/config.py:load_settings()`, so env vars must be set before importing it (see `tests/conftest.py`). Every `/players/...` route checks the linked chess account belongs to the signed-in user; Pro routes check limits from `app/plans.py`.
- **Storage:** a single SQLite file (`DB_PATH`) through `app/store.py`. Schema is `CREATE TABLE IF NOT EXISTS` plus additive `ALTER TABLE` steps in `Store._migrate` — add new columns there so older databases upgrade.
- **Job queue (`app/jobs.py`, handlers in `app/sync.py`):** all engine-heavy work (imports, Prep Check, previews, Twin builds, etc.) is queued in the DB. `JOB_MODE` = `thread` (workers in the web process, default), `external` (web only enqueues; `app.worker` runs jobs — production), `inline` (runs within the request — used by tests so results are visible immediately). Long imports run in chunks (`IMPORT_CHUNK`) and re-queue themselves for fairness.
- **Analysis pipeline:** `sources.py` (Lichess/Chess.com fetch, rate-limited, incremental) → `games.py` (PGN parsing) → `engine.py`/`analysis.py` (Stockfish win %, accuracy, classification, tags) → `profile.py` (Rating DNA, plan) → `player_bundle.py` writes an **OKF v0.2 knowledge bundle** per account (`okf.py` reads/writes these: markdown concepts with YAML frontmatter) under `<DB dir>/bundles/`. `app/knowledge/` is the hand-authored principles bundle mounted at `/knowledge`.
- **Coach (`coach.py`, `llm.py`, `answer.py`, `grounding.py`):** the LLM navigates the OKF bundle via tools (`read_index`, `read_concept`, `search_concepts`, `find_moments`, `analyse_position`), must reply through a Pydantic-validated `final_answer` tool, and every move/eval/link in the answer is checked against what it read; failures get one rewrite, then ⚠ flags. `llm.py` picks the provider (`COACH_PROVIDER`: Anthropic, OpenAI-compatible, Ollama, fallback provider, offline rule-based coach). Per-plan daily token budgets apply.
- **Feature modules** (each with its own `test_*.py` and usually a `views/*.js`): `twin.py` (opponent-imitating bot), `library.py` (opponent library + position search), `scout.py`, `prep.py`, `repertoire.py`, `progress.py`, `predict.py` + `puzzlebank.py` (predicted puzzles), `think.py`, `scoresheet.py` (photo → PGN via vision models), `review.py`/`review_ai.py` (report cards), `openings.py`.
- **Kill switches (`app/switches.py`):** DB-stored flags read by every process; new risky features get a switch entry plus a `PAUSED_MESSAGES` string.
- **Front end:** `app/static/js/main.js` → `shell.js` routes to `views/*.js`; `board.js` is the SVG board; `vendor/chess.js` for move legality. No bundler.

## Conventions when adding files / releasing

- New static JS/CSS files must be added to the `SHELL` list in `app/static/sw.js`; bump its `VERSION` on release so clients drop stale caches.
- New tracked files must be added to the `RELEASE` set in `scripts/stale_files.py` (it lists files not in the current release).
- New env vars: add to `app/config.py`, the README settings table, and `deploy/.env.example`; set a fast/safe value in `tests/conftest.py` if it controls engine depth or external calls.
- Tests never touch real LLMs, vision models, SMTP or network — conftest strips those env vars and uses `COACH_PROVIDER=offline` and tiny engine depths. Shared fixtures `engine`, `demo_games`, `demo_bundle` use `sample_data/demo_games.pgn`.
- Production deps are pinned with hashes in `requirements.lock`; regenerate after editing `requirements*.txt` (command at the top of that file).

## Rules

- Before saying a task is done, run `python -m pytest -q`, `ruff check .`, and `node --check` on every changed JS file. If one can't run (e.g. Node isn't installed), say so rather than claiming it passed.
- Never put API keys, passwords or other secrets in code; they come from environment variables (`app/config.py`, `deploy/.env.example`).
- All UI work follows DESIGN.md.
- Keep the design our own: don't copy Chess.com's look, layout, assets or wording.
- Commit with clear messages that say what changed and why.
