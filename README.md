# Plateau Breaker

An AI chess coach that learns from **your own games**. Link your Lichess or Chess.com account and it will:

1. **Fetch your games automatically.** No copy-pasting PGNs. Incremental sync picks up only new games.
2. **Analyse every move** with Stockfish and build your **Rating DNA**: a rating per skill (openings, middlegame, endgames, tactics, time management, converting wins, defending).
3. Turn your mistakes into **puzzles** on a spaced-repetition schedule, and write a **7-day study plan**.
4. Store everything it learns as an **OKF knowledge bundle** (Google's Open Knowledge Format): plain markdown concepts with YAML frontmatter that you can browse in the app, download, and version in git.
5. Let you **chat with a grounded AI coach** that navigates that bundle, links to the exact concepts it uses (click to see the board), and has every move or evaluation it states checked before you see it.
6. Import **ChessBase** games (export to PGN), and with Pro, run **Prep Check**: compare your ChessBase repertoire with the games you actually played.

**Pro features:** Prep Check, Opponent Scout, Repertoire Leaks (including recurring mistakes), Progress tracking and Auto-sync.

## What's new in v0.5

* **New interface** built around four jobs: **Diagnose, Train, Prepare, Coach** (bottom tab bar on phones).
  "Your scoresheet, marked up by a coach": blue ink is you, red pen is the coach.
* **Instant preview on the landing page:** type a Lichess username, get a Rating DNA from the last 15 games
  in about half a minute, no account needed. Signing up from there links that account and starts the full import.
* **Interactive board** everywhere: drag or click to move (or type the move), arrows for your move and the
  engine's, and a **game viewer** with a scoresheet that jumps to your costliest mistake.
* **Shareable Rating DNA card** (PNG for WhatsApp/Instagram/X), **Pro previews** that show real counts from
  your games ("10 opening lines to check") instead of hidden tabs, and coach answers with the cited positions on boards.
* **Installable app (PWA)**, dark mode, self-hosted fonts (no Google requests).
* **Job queue:** imports, Prep Checks and previews run on a persistent, fair queue. Long imports go in chunks
  of 20 newest-first, so the dashboard fills in after the first chunk; previews have their own fast lane.
* **Email verification and password reset**, a **support form**, **account deletion**, a **weekly digest email**.
* **Second model provider** (`LLM_FALLBACK_*`) tried before the offline coach.
* **Sentry** error monitoring, **Plausible/PostHog** analytics, a public **status page** at `/status`.
* **Daily backups** to S3-compatible storage (optionally encrypted) with a **weekly restore test**.
* **Production deployment** in `deploy/`: Caddy (automatic HTTPS), Docker Compose with separate web,
  worker and scheduler services, a VPS setup script and systemd units. See **DEPLOY.md**.
* **Plans:** Free, Pro, **Event Pass** (Pro for 10 days: `scripts/admin.py grant EMAIL --days 10`) and a Coach waitlist.

---

## Quick start in GitHub Codespaces

1. Open the repo → **Code → Codespaces → Create codespace** (setup installs Stockfish and Python packages).
2. Run:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
3. Open port 8000. Try the preview with any Lichess username, or **create an account** and link your account.

### Turn on real email (about 5 minutes)

Until email is set up, verification and password-reset emails are saved as `.eml` files in the `outbox/`
folder next to the database instead of being sent. To send them for real from Codespaces, use Gmail:

1. Turn on **2-Step Verification** in your Google Account (Security), then create an **App Password** at
   <https://myaccount.google.com/apppasswords> (name it "Plateau Breaker"). Copy the 16 letters.
2. On GitHub: **Settings → Codespaces → Secrets → New secret**, with access to this repository:

   | Secret | Value |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | your full Gmail address |
   | `SMTP_PASSWORD` | the App Password (spaces are fine) |
   | `SUPPORT_EMAIL` | where support messages should go (e.g. the same Gmail) |

3. Reload the codespace when VS Code offers to (or stop and restart it), so the secrets reach the terminal.
   Then check: `env | grep SMTP_HOST`.
4. Test, then start the app:
   ```bash
   python scripts/admin.py test-email you@gmail.com
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   If the test fails, it says what to change. `/status` should now show **Email: Working**. Signed in as
   an `ADMIN_EMAILS` user, **Account → Email sending** has the same test as a button.

Links in emails point at your codespace's address automatically. The port is private, so they open for you
(signed in to GitHub) while the codespace is running; to let someone else test, make port 8000 **Public**
in the Ports tab. Gmail allows about 500 recipients a day, which is plenty for testing. For launch, send
from your own domain instead (Brevo or another provider, see DEPLOY.md).

To give yourself Pro while testing, add a Codespaces secret `ADMIN_EMAILS` with your email *before* you sign up. Or grant it afterwards with `python scripts/admin.py grant you@example.com`.

## The AI coach: pick a model

Without any setup, the Coach uses a built-in rule-based coach (with citations). For the AI coach, add **one** of these as Codespaces secrets, then rebuild:

| Option | Secrets | Notes |
|---|---|---|
| Groq / Gemini / OpenRouter (free tiers) | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Groq: `https://api.groq.com/openai/v1`, e.g. `openai/gpt-oss-120b`. Free tier: 8K tokens/minute, 200K/day; the coach waits out per-minute limits automatically |
| Claude | `ANTHROPIC_API_KEY` (optional `COACH_MODEL`) | Best quality, paid |
| Ollama (local, free) | none: run `bash scripts/setup_ollama.sh` | Slow on CPU; small models |

## Knowledge base: OKF bundles

Each linked account gets an [OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog) bundle, rebuilt after every sync, stored next to the database (`<DB folder>/bundles/<account>/`):

```
index.md                 root index (okf_version: "0.2"), progressive disclosure
log.md                   dated history of updates
player.md                Player Profile: entry point (ratings, findings, links)
skills/<skill>.md        Skill Assessment
patterns/<pattern>.md    Mistake Pattern, linked to the principle behind it
moments/<game>-<ply>.md  Critical Moment: FEN, move played, engine best + line, evals (all in frontmatter)
games/<game>.md          Game
openings/<colour>/...    Opening Line (Pro)
recurring.md, progress.md, prep/<colour>-<name>.md   (Pro)
```

A hand-authored **chess-principles bundle** (`app/knowledge/`) is mounted at `/knowledge`. Trust follows the spec: engine-derived concepts are `verified: {by: process:stockfish}` (machine-confirmed); principles are **unverified** until a strong player reviews them and adds `verified: {by: human:<you>, at: ...}`. Both bundles pass Google's reference OKF parser.

The coach reads the bundle the way the spec intends: it starts from `/player.md`, uses `read_index` and `read_concept` to follow links, and `search_concepts` / `find_moments` (frontmatter queries) for specific lookups. Download any account's bundle from the **Knowledge** tab.

## ChessBase

**Games:** in ChessBase, create a PGN database (File > New > Database, choose PGN) and copy your games into it, then upload the `.pgn` (or a `.zip` of several) to a **ChessBase / PGN** account. Enter your name as in the file: "Sikka, Aanya" and "Aanya Sikka" both match. Windows-encoded files (accents in names) are handled. Native `.cbh`/`.cbv` files can't be read; the app tells you how to export.

**Prep Check (Pro):** upload your repertoire the same way, with all its variations, one colour per file (colour is detected automatically). It reports:
- **Deviations:** positions where your prep has a move and you played something else in a game.
- **Gaps:** opponent moves your prep doesn't cover, with an engine suggestion for what to add.
- **Holes:** prepared lines whose final position the engine thinks is bad for you (-0.5 or worse).
- **Drills:** "what's your prep here?" positions added to the puzzle deck, starting with the ones you got wrong. Any prepared move counts as correct.

Reports re-run automatically whenever new games are synced.

## How the coach avoids hallucinating

```
question ─► navigate the OKF bundle ─► generate ──────────► verify ──────────► answer
            • brief = /player.md        • must link concepts    • every move,       • ✓ verified, or
            • read_index / read_concept • may call Stockfish      eval and link       ⚠ flagged items
            • search_concepts             via analyse_position    checked against
            • find_moments (frontmatter)                          what was read
            • /knowledge principles                             • fail → one rewrite
```

- **Knowledge (`app/okf.py`, `app/player_bundle.py`):** every mistake and blunder is a Critical Moment concept whose frontmatter holds the position, the move played, the engine's move and line, and the evaluations. Answers cite concepts with ordinary markdown links, e.g. `[29...hxg6 vs bob](/moments/abc-56.md)`.
- **Structured answers (`app/answer.py`):** the model never replies in free text. It must call a `final_answer` tool whose fields (summary, up to four evidence points with concept links, one next step) are validated with **Pydantic**. Reasoning, `<think>` blocks and "let me check…" narration never reach the screen; an invalid form is sent back to the model to fix.
- **Grounding (`app/grounding.py`):** before an answer is shown, every piece move, capture, castling move, numbered pawn move, evaluation and concept link in it must appear in what the coach read. If not, the model is told exactly what failed and gets one rewrite. Anything still unverified is shown with a ⚠ warning, never passed off as fact.

## Plans

| | Free | Pro |
|---|---|---|
| Games per sync | 50 | 300 |
| Linked accounts | 2 | 6 |
| Coach questions / day | 15 | 200 |
| Rating DNA, puzzles, plan | ✓ | ✓ |
| Opponent Scout | – | ✓ (25/day) |
| ChessBase / PGN upload, OKF bundle export | ✓ | ✓ |
| Prep Check (ChessBase repertoire vs games) | – | ✓ |
| Repertoire Leaks + recurring mistakes | – | ✓ |
| Progress tracking | – | ✓ |
| Auto-sync | – | ✓ |

Limits live in `app/plans.py`. Payments aren't wired up: users press **Request Pro**, you see requests with `python scripts/admin.py requests` and grant with `... grant EMAIL`. See `DEPLOY.md` for adding Stripe or Razorpay.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `STOCKFISH_PATH` | auto-detected | Path to Stockfish |
| `ENGINE_DEPTH` / `ENGINE_TIME` | `12` / `0.08` | Search limit per position |
| `MAX_GAMES` | `300` | Server-wide cap on games per sync (cost control) |
| `MAX_CONCURRENT_IMPORTS` | `2` | Analyses running at once |
| `DB_PATH` | `./plateau.db` | SQLite file |
| `ADMIN_EMAILS` | unset | Comma-separated; these get Pro + admin on signup |
| `COOKIE_SECURE` | `0` | Set `1` in production (HTTPS) |
| `ENABLE_AUTOSYNC` / `AUTOSYNC_INTERVAL_MIN` | `1` / `360` | Pro auto-sync loop |
| `CHESSCOM_ENABLED` | `1` | Set `0` until Chess.com approves commercial use (see DEPLOY.md) |
| `CONTACT_EMAIL` | unset | Sent in the User-Agent to Lichess/Chess.com, as Chess.com requests |
| `PRO_PRICE_LABEL` | `₹399 / $6 a month` | Price shown on the Plans tab |
| `COACH_PROVIDER` | `auto` | `auto`, `anthropic`, `openai`, `ollama`, `offline` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | unset | OpenAI-compatible provider |
| `LLM_REASONING_EFFORT` | `low` for gpt-oss models | How long the model thinks; `low` saves free-tier tokens |
| `ANTHROPIC_API_KEY` / `COACH_MODEL` | unset / `claude-sonnet-5-5` | Claude |
| `JOB_MODE` | `thread` | `thread`: workers inside the web process. `external`: run `python -m app.worker` separately (production) |
| `IMPORT_CHUNK` | `20` | Games analysed per turn before a long import yields the queue |
| `PREVIEW_GAMES` / `PREVIEW_DEPTH` / `PREVIEWS_PER_HOUR` | `15` / `8` / `6` | Landing-page preview size, depth, per-IP limit |
| `PUBLIC_URL` | `http://localhost:8000` | Base URL used in email links |
| `SUPPORT_EMAIL` | unset | Shown in the app; support messages are emailed here |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_SECURITY` | unset / `587` / … / `starttls` | Outgoing email; without it, emails go to `outbox/` |
| `REQUIRE_EMAIL_VERIFICATION` | `0` | `1` blocks syncing and the coach until the email is confirmed |
| `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_API_KEY` / `LLM_FALLBACK_MODEL` | unset | Second OpenAI-compatible provider |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` / `SENTRY_TRACES_SAMPLE_RATE` | unset / `production` / `0` | Error monitoring |
| `ANALYTICS` / `PLAUSIBLE_DOMAIN` / `POSTHOG_KEY` / `POSTHOG_HOST` | `none` | Privacy-friendly analytics |
| `BACKUP_TARGET` / `BACKUP_KEEP` / `BACKUP_ENCRYPTION_KEY` / `S3_ENDPOINT_URL` / `S3_REGION` | unset / `14` | Daily backups (see DEPLOY.md) |
| `BACKUP_HOUR_UTC` / `DIGEST_HOUR_UTC` | `21` / `3` | When the scheduler runs backups and Monday's digest |

## Code map

| File | Job |
|---|---|
| `app/sources.py` | Lichess + Chess.com download, incremental `since`, one request at a time per platform, 60 s pause after a 429 |
| `app/games.py` | PGN parsing, ChessBase uploads (.pgn/.zip, Windows encoding), name matching, time classes |
| `app/engine.py` / `app/analysis.py` | Stockfish wrapper; per-move win %, accuracy, classification, pattern tags |
| `app/profile.py` | Rating DNA, insights, weekly plan |
| `app/okf.py` | OKF v0.2 bundle writer/reader: frontmatter, index.md, log.md, links, trust tiers, staleness, search, conformance |
| `app/player_bundle.py` | Writes each player's analysis as an OKF bundle |
| `app/knowledge/` | The chess-principles OKF bundle (hand-authored, unverified until reviewed) |
| `app/grounding.py` | Checks moves, evaluations and concept links in coach answers |
| `app/prep.py` | ChessBase repertoire parsing and Prep Check |
| `app/coach.py` | Coach that navigates the OKF bundle, with a verify-and-repair loop; offline coach |
| `app/llm.py` | Picks the model provider; OpenAI-compatible tool calling |
| `app/repertoire.py` | Opening tree, leaks, recurring mistakes (Pro) |
| `app/progress.py` | Monthly/weekly trends (Pro) |
| `app/scout.py` | Opponent report: lines, weak spots, habits, battleground, engine-checked prep (Pro) |
| `app/jobs.py` | Persistent job queue: fair claiming, chunk re-queueing, crash recovery, worker threads |
| `app/sync.py` | Job handlers (chunked import, Prep Check, instant preview), auto-sync |
| `app/worker.py` / `app/scheduler.py` | Separate worker process; daily backup, weekly restore test, weekly digest |
| `app/mailer.py` / `app/digest.py` | SMTP email (or dev outbox) and the weekly digest |
| `app/backup.py` / `scripts/backup.py` | Snapshot, encrypt, upload, prune, restore, verify |
| `app/status.py` / `app/observability.py` | Status page checks; Sentry, browser-error relay, analytics config |
| `app/static/js/` | Front end as ES modules: `board.js` (SVG board), `dna.js` (chart + share card), `views/` |
| `deploy/` | Caddyfile, docker-compose.yml, .env.example, setup-vps.sh, systemd units |
| `app/auth.py` / `app/plans.py` / `app/store.py` | Accounts & sessions, plan limits, SQLite |
| `app/main.py` | HTTP API with ownership and plan checks |
| `scripts/admin.py` | Users, upgrade requests, grant/revoke Pro |

## Tests

```bash
pip install -r requirements-dev.txt
pytest        # 172 tests; engine tests skip if Stockfish is missing
ruff check .
```

The demo data (`sample_data/demo_games.pgn`) is 40 synthetic games for `demo_player`. Use real games to judge the product.
