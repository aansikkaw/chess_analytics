# Plateau Breaker

An AI chess coach that learns from **your own games**. Import your Lichess games (or paste a PGN) and it will:

1. **Analyse** every position with Stockfish and judge each of your moves.
2. Build your **Rating DNA**: a rating per skill (openings, middlegame, endgames, tactics, time management, converting wins, defending) so you see what's actually capping you.
3. Turn your mistakes into **puzzles**, reviewed on a spaced-repetition schedule (1 → 3 → 7 → 14 → 30 days).
4. Write a **7-day study plan** aimed at your two weakest skills.
5. Let you **chat with a coach agent** that answers from your data and verifies every move with the engine.

## Run it in GitHub Codespaces (nothing installed on your laptop)

1. Create a new **private** repository on GitHub named `chess_analytics` (tick "Add a README file"), open a codespace on it, drag `chess_analytics.zip` into the file list, then run `unzip chess_analytics.zip && cp -r chess_analytics/. . && rm -rf chess_analytics chess_analytics.zip` and rebuild the container.
2. On the repo page: **Code → Codespaces → Create codespace on main**.
3. Wait for setup to finish (first time takes a few minutes: it installs Stockfish and the Python packages automatically).
4. In the terminal at the bottom, run:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
5. A browser tab opens with the app (or open the **Ports** tab and click the globe next to port 8000).
6. Optional AI coach: add `ANTHROPIC_API_KEY` under **GitHub → Settings → Codespaces → Secrets**, give it access to this repo, then rebuild the codespace. Never paste the key into the code.

When you're done, **stop the codespace** (Codespaces menu → Stop) so it doesn't use up your free hours. Your files and database are kept until you delete it.

## Turn on the AI coach (free options)

Without any setup, the Coach tab uses a built-in rule-based coach. To get a real AI coach, pick **one** option. The app detects it automatically; the header shows which model is active.

### Option A: Ollama, free and local (no account, no key)
A small open model runs inside your Codespace.

```bash
bash scripts/setup_ollama.sh
```
This installs Ollama, starts it and downloads `qwen2.5:3b` (about 2 GB, first time only). Then restart the app. Ollama starts by itself whenever the Codespace starts. After a **Rebuild Container**, run the script again.

- Speed: on the default 2-core Codespace, an answer takes roughly 30–90 seconds. A 4-core machine is about twice as fast but uses free hours twice as fast.
- Quality: a 3B model is far weaker than Claude or other large hosted models. The app compensates by giving it your verified stats and costliest moments up front, so it explains real data rather than inventing lines.
- Other models: `OLLAMA_MODEL=llama3.2:3b bash scripts/setup_ollama.sh`, then set `OLLAMA_MODEL` the same way when you start the app.

### Option B: a hosted model with a free tier (fast, needs a free key)
Any provider with an OpenAI-compatible API works. Add these three as **Codespaces secrets** (GitHub → Settings → Codespaces → Secrets), give them access to this repo, then rebuild the codespace:

| Secret | Example (Groq) | Example (Google Gemini) |
|---|---|---|
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_API_KEY` | your Groq key | your Gemini API key |
| `LLM_MODEL` | a model from their list that supports tool calling | e.g. a current Flash model |

Free tiers and model names change; check the provider's docs for current limits and pick a model that supports **tool/function calling**.

### Option C: Claude (paid, best quality)
Add `ANTHROPIC_API_KEY` as a Codespaces secret.

### Which one wins
With `COACH_PROVIDER=auto` (the default) the app uses: Claude key → OpenAI-compatible settings → running Ollama → built-in coach. Force one with `COACH_PROVIDER=anthropic|openai|ollama|offline`. If the chosen model fails (rate limit, timeout, missing model), you still get the built-in coach's answer, plus a one-line note saying what went wrong.

Never paste keys into the code or commit them; use secrets.

## Run it on your own machine

You need Python 3.10+ and Stockfish.

```bash
# 1. Stockfish
brew install stockfish            # macOS
# sudo apt install stockfish      # Ubuntu/Debian
# Windows: download from stockfishchess.org, then set STOCKFISH_PATH to the .exe

# 2. Python packages
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. (optional) switch the coach from offline mode to the AI agent
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Start
uvicorn app.main:app --reload
```

Open http://localhost:8000, pick **Demo data** and click **Analyse games** to try it with no account. Then try your own Lichess username.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `STOCKFISH_PATH` | auto-detected | Path to the Stockfish binary |
| `ENGINE_DEPTH` / `ENGINE_TIME` | `12` / `0.08` | Search limit per position (whichever hits first). Higher = slower, more accurate |
| `MAX_GAMES` | `60` | Cap on games per import |
| `DB_PATH` | `./plateau.db` | SQLite file |
| `COACH_PROVIDER` | `auto` | `auto`, `anthropic`, `openai`, `ollama` or `offline` |
| `ANTHROPIC_API_KEY` / `COACH_MODEL` | unset / `claude-sonnet-5-5` | Claude coach |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | unset | Any OpenAI-compatible provider |
| `OLLAMA_HOST_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `qwen2.5:3b` | Local Ollama |

40 games at the defaults take about 30–40 seconds.

## How it works

```
Lichess API / PGN ──► games.py ──► analysis.py ──► store.py (SQLite)
                     parse, clocks   Stockfish per position      │
                                     win%, accuracy, tags        ▼
                                                     profile.py: Rating DNA, insights, plan
                                                                 │
                         main.py (FastAPI) ◄─────────────────────┤
                         static/ (UI)              coach.py: agent with tools
```

| File | Job |
|---|---|
| `app/games.py` | Downloads games from Lichess, parses PGN, reads `[%clk]` clock times, works out which side you played |
| `app/engine.py` | Wraps one Stockfish process; scores always from White's view, mates mapped to ±10,000 |
| `app/analysis.py` | Per-move judgment using Lichess's maths: centipawns → win %, move accuracy, inaccuracy/mistake/blunder at 10/20/30 points lost. Tags *why* a move went wrong: missed tactic, hanging piece, missed mate, time trouble, failed conversion |
| `app/profile.py` | Rating DNA, plain-language insights, and the weekly plan |
| `app/store.py` | SQLite tables for analysed games and the puzzle deck (Leitner-box spaced repetition) |
| `app/llm.py` | Picks the coach's model (Claude, OpenAI-compatible, Ollama or offline) and handles the OpenAI-style tool-calling protocol |
| `app/coach.py` | The coach. With a model configured: an LLM agent that must call tools (`get_rating_dna`, `list_critical_moments`, `analyse_position`...) and may only mention moves a tool returned. Without a key: a rule-based coach on the same tools |
| `app/main.py` | HTTP API, background import jobs, puzzle checking |
| `scripts/make_demo_pgn.py` | Generates the synthetic demo games |

### Rating DNA, in one paragraph
Your real rating is the anchor. For each skill, the app takes your average move accuracy in that kind of position and compares it with your overall accuracy; every accuracy point above or below average moves the skill rating 12 points. Moves played when the game was already decided (beyond ±6 pawns) are left out, because almost any move scores ~100% there and that would hide real weaknesses. It's a heuristic, not a true Elo per skill; see `REVIEW.md` for how to calibrate it.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/import` | `{"source": "lichess" \| "pgn" \| "demo", "username": "...", "pgn": "...", "max_games": 40}` → `job_id` |
| GET | `/api/jobs/{id}` | Import progress |
| GET | `/api/players/{user}/profile` | Rating DNA, insights, recent games (`?rating=` overrides the anchor) |
| GET | `/api/players/{user}/puzzles` | Due puzzles (answers not included) |
| POST | `/api/puzzles/{id}/attempt` | `{"move": "e2e4" or "Nf3"}` → verdict, solution, next review |
| GET | `/api/players/{user}/plan` | 7-day plan |
| POST | `/api/players/{user}/coach` | `{"message": "...", "history": [...]}` |
| GET | `/api/health` | Engine and coach status |

## Tests

```bash
pip install -r requirements-dev.txt
pytest          # 33 tests; engine tests skip if Stockfish is missing
ruff check .
```

## About the demo data
`sample_data/demo_games.pgn` is 40 synthetic 15+10 games: Stockfish limited to ~1800 for `demo_player`, dropped to its weakest setting under 60 seconds on the clock, and to ~1400 in endgames. The time-trouble weakness shows up clearly in the analysis. The endgame one mostly doesn't, because even weakened Stockfish plays simple endgames well. Use real games to judge the product.
