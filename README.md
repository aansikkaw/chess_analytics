# Plateau Breaker

An AI chess coach that learns from **your own games**. Link your Lichess or Chess.com account and it will:

1. **Fetch your games automatically.** No copy-pasting PGNs. Incremental sync picks up only new games.
2. **Analyse every move** with Stockfish and build your **Rating DNA**: a rating per skill (openings, middlegame, endgames, tactics, time management, converting wins, defending).
3. Turn your mistakes into **puzzles** on a spaced-repetition schedule, and write a **7-day study plan**.
4. Store everything it learns as an **OKF knowledge bundle** (Google's Open Knowledge Format): plain markdown concepts with YAML frontmatter that you can browse in the app, download, and version in git.
5. Let you **chat with a grounded AI coach** that navigates that bundle, links to the exact concepts it uses (click to see the board), and has every move or evaluation it states checked before you see it.
6. Import **ChessBase** games (export to PGN), and with Pro, run **Prep Check**: compare your ChessBase repertoire with the games you actually played.
7. **Mark any game like a test paper:** photograph a scoresheet or paste a game and get a **report card**: every move of both players classified by Stockfish, grades by phase, key moments on the board, the opening's name, a written review from the coach, and an annotated PGN. No ChessBase needed.

**Pro features:** Prep Check, Opponent Scout and Library, Opponent Twins, Repertoire Leaks (including recurring mistakes),
Progress tracking, Auto-sync and higher daily allowances. The **Coach** plan adds a dashboard for up to 20 students.
See [Plans](#plans).

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

## New in v1.2: game report cards, and a sharper scoresheet reader

Like a teacher marking a test paper: give it any game, from a photo of a scoresheet, a typed or pasted
game, or one of your synced games, and it comes back marked move by move, with a written review. It is
the analysis a beginner would otherwise buy ChessBase for.

### Game report card (Diagnose → Report card, "Full report card" in the game viewer, or straight after a scan)

* **Both players, every move.** Stockfish checks every position with its two best moves (depth
  `REVIEW_DEPTH`, default 14: about 10–20 seconds for a game).
* **Move classes:**
  - **Brilliant:** a sound piece sacrifice that is also the best move.
  - **Great find:** the only good move, and it was found.
  - Best, Excellent, Good.
  - **Book:** still in a named opening line.
  - **Forced:** the only legal move.
  - Inaccuracy, Mistake and Blunder.
  - **Missed chance:** the opponent's mistake wasn't punished.

  Walking into a forced mate is always a blunder, even in a lost position.
* **Grades:**
  - Accuracy per player (Lichess's formula).
  - Average centipawn loss.
  - A grade from A to F overall and for the opening, middlegame and endgame.
* **The dashboard:**
  - **Evaluation graph:** click it to jump to that move.
  - **The board:** the move played (blue arrow) and the better one (red arrow), plus the evaluation after
    every move.
  - **Key moments:** each with the engine's line, the reply that punishes it, and a plain reason: a piece
    left hanging, a missed mate, a missed tactic, a won position let slip, a mistake not punished, time
    trouble.
  - **The turning point** and a one-line verdict.
* **Opening names with ECO codes,** from Lichess's open opening list (CC0, about 3,800 lines,
  `app/data/openings.tsv`). Openings are matched by position, so transpositions are found. The card also
  shows the move where the game left known theory.
* **The coach's review:**
  - A summary, a paragraph for each phase, and what went well.
  - Lessons, each with **Show me**.
  - What to train next, with links to Predicted for you, Think here and your mistakes.

  It uses your AI model if one is set up (the same `LLM_*` / `ANTHROPIC_API_KEY` as the coach). The model
  only gets the engine's facts and is told not to analyse. Every move and evaluation it writes is checked
  against them: it gets one rewrite, then any sentence that still doesn't check out is removed. Without a
  model, or if it fails, the built-in coach writes the same sections.
* **Download annotated PGN.** It has !, ?, ?? marks, `[%eval]` for every move (mates as `#3`), and the
  better move and engine line as comments. It opens in ChessBase, a Lichess study or any chess app.
* **Daily allowance:**

  | Plan | Reviews | Written by the AI |
  |---|---|---|
  | Free | 10 | 3 |
  | Pro | 100 | 30 |
  | Coach | 200 | 60 |

  Kill switch: `review`.

### A sharper photo reader

1. **Computer vision in your browser**, before anything is uploaded:
   - It finds the sheet's corners and flattens the perspective.
   - It removes shadows (dividing by the blurred background) and boosts the contrast.
   - **Fit the corners to the sheet** shows the outline, with corners you can drag (or move with the arrow
     keys) and a live preview. **Use the whole photo** skips the outline.
2. **Zoomed columns.** The reader gets the flattened sheet plus zoomed copies of its two halves (the two
   move columns) in one request.
3. **Two readers.** Set `VISION2_MODEL` / `VISION2_BASE_URL` / `VISION2_API_KEY` to a second vision model.
   Both read the sheet, and their readings are merged by vote.
4. **A second look.** Moves that are still unclear are asked about again as multiple choice, with the legal
   moves in that position as the options.
5. **A human-move prior.** Where a move is still unclear, Stockfish's idea of what a club player would play
   in that position breaks the tie. A move 10 points worse in expected score is taken as about half as
   likely.

**Which vision models to use:**

* **Free:** your Groq key (the default).
* **Better:** in AIMultiple's OCR benchmark, OpenAI's GPT-5 (95%) and Gemini 2.5 Pro (93%) scored near the
  top for handwriting. Use one as the main reader and the other as `VISION2_*`. Both work through their
  OpenAI-compatible endpoints:
  - **Gemini:** `VISION_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`,
    `VISION_MODEL=` a current Gemini model from Google's list (for example `gemini-3.8-flash`),
    `VISION_API_KEY=` a Google AI Studio key.
  - **OpenAI:** `VISION2_BASE_URL=https://api.openai.com/v1`, `VISION2_MODEL=` a GPT vision model,
    `VISION2_API_KEY=` your OpenAI key.
* **Claude** reads photos when `ANTHROPIC_API_KEY` is set and no `VISION_*` is.

## New in v1.1: three ways to train that no other chess site has

### 1. Predicted for you: puzzles from the mistakes you haven't made yet (Train → Predicted for you)

Other sites serve puzzles at your rating. This picks the ones **you** are likely to get wrong, before a game
does it for you, and says why.

* **The puzzles** are Lichess's open database (CC0, millions of rated, well-tested puzzles), loaded with
  `python scripts/import_puzzles.py --download`. A sample of 365 puzzles of our own ships with the app, so
  it works before that import.
* **Your Twin.** The Opponent Twin's model, fitted to *your own* analysed games. It learns your instincts:
  captures or quiet moves, checks, trades, retreats, recaptures, king hunts. Build it with **Build my Twin**
  (Free: 60 games, Pro: 300).
* **The prediction** for each puzzle is `logit P(miss) = (puzzle rating − your rating)·ln10/400 − theme
  offset + blind spot`:
  - The rating part is what a typical player at your level would do.
  - The theme offset comes from your Rating DNA (e.g. weaker endgames), then from your own puzzle results.
  - The **blind spot** compares how much attention *your* Twin's instincts give the winning move with how
    much a typical player's Twin gives it (the average of every Twin on the server near your rating). The
    loss weights are left out on purpose: a puzzle hides how much each move loses, and that is exactly
    what the puzzle rating measures.
* **Shared work.** Each bank puzzle is annotated once with Stockfish's candidate moves, and everyone reuses
  that. Scoring your Twin on a thousand puzzles then takes milliseconds.
* **What you get:** puzzles you'd miss 35–88% of the time where the *personal* part is largest, mixed across
  motifs. Before you answer you see the odds and a spoiler-free reason. Afterwards you see the full reason
  ("Your Twin's instinct is to look at Bxd5 first; it gives the winning move 9% of its attention; a typical
  player 21%. Why: you like to grab material").
* **Honest scoring.** Your puzzle rating moves with every first try (Glicko-1). The page shows expected
  versus actual misses and a Brier score, so you can see how well it predicts you. Misses go into the
  spaced-repetition deck.
* **Daily allowance:** Free 8 new puzzles a day, Pro 40, Coach 60.

### 2. Think here: learn when a position needs a long think (Train → Think here)

* From the clock times in your games (`clock before − clock after + increment`), how long you took over
  every move. Stockfish's top three moves show which positions were **critical**: the best move is 15+
  win-% points ahead of the second. Plain recaptures and forced replies don't count, because playing those
  fast is right.
* **Rushed** = a critical moment played under 60% of your usual time per move (2–10 s). **Over-thought** = 3×
  your usual time (12 s or more) on a routine move.
* **Stats:**
  - Critical moments rushed.
  - Points per 10 games lost by rushing: your loss when rushing beyond your loss when you do think, turned
    into a rating estimate with the Elo formula.
  - Time on critical versus routine moves.
  - How many rushes happened with plenty of time on the clock.
  - A chart of your median think time from routine positions to only-moves.
* **The drill:** each position first asks "Stop and think, or just play?" (keys T / P). Critical: find the
  move (anything within 3 points of the best counts). Routine: see the moves that were all fine. Answers
  are scheduled with spaced repetition. Free scans 100 games, Pro 500.

### 3. Scoresheet scanner: over-the-board games from a photo (Scan, in the side menu)

* **Photo or typed.** Photograph a handwritten scoresheet, or type the moves in **any notation language**:
  English, German (S L T D), French (C F T D R), Spanish/Italian, Portuguese, Dutch, Russian, or figurines.
  0-0, `:` captures, long algebraic (Ng1-f3, e2e4), missing x's and dropped disambiguations all work.
* **The rules of chess do the hard part.** A vision model reads each move with up to two alternative
  readings. A **beam search over the whole game** then keeps only legal sequences, using a handwriting
  confusion model (b/6, g/9, l/1, O/0, e/c…). A smudged move is settled by the moves after it.
* **Every move gets a confidence:** clear, worth a look, or a guess. Click one to see the position, the
  reading and the legal alternatives, or play the right move on the board. The rest of the game is
  re-decoded to fit.
* **Save it** to your over-the-board account (analysed like any other game: Diagnose, puzzles, your Twin)
  and/or to your Opponent Library under your opponent's name (Pro). You can also download the PGN.
* **Take a photo** opens a live camera view inside the page (laptop webcam or phone camera), with a frame
  to fit the sheet in, then lets you check the shot and retake it. Browsers without camera access fall back
  to the phone's camera app; **Choose a photo** uploads one you already have.
* **Photos are read and dropped**, never stored. Free 3 photos a day, Pro 30 (typing is unlimited).
* **Which model reads photos:**
  - `VISION_BASE_URL` / `VISION_MODEL` / `VISION_API_KEY` for any OpenAI-compatible vision model.
  - Otherwise Claude, if `ANTHROPIC_API_KEY` is set.
  - Otherwise, with your Groq key, Groq's vision model (`qwen/qwen3.8-27b`; override with `VISION_MODEL`).

**Kill switches:** `predicted`, `think` and `scanner` (`python scripts/admin.py switch scanner off`).

## New in v1.0: Deep Twin (a player's whole career, not just 200 games)

* **Download full history.** On a library player's page, one click downloads their entire rated history in
  the background: Lichess page by page, Chess.com month by month. It resumes after interruptions, waits
  politely when the sites rate-limit, and Update keeps it current afterwards. Pro keeps up to 3,000 games
  per player and Coach up to 10,000 (`library_full_games` in `app/plans.py`, server cap `LIBRARY_FULL_MAX`).
* **Their book uses every game.** The Explorer and the Twin's opening book read all of a player's saved
  games, weighting recent ones. This needs no engine, so it stays instant at 10,000 games.
* **The model keeps learning.** Running Stockfish over every game is the expensive part (about 1 second per
  game). So each game is studied **once, ever**, and the result is saved compressed (about 2 KB per game).
  - A build studies the next batch (150 games by default). Two thirds are the newest unstudied games and one
    third is spread across their history.
  - **Keep learning** adds another batch, up to the plan's limit: Pro 600 studied games, Coach 2,000.
  - Refitting on everything studied takes seconds.
* **Recent games count more.** In the model a game two years older counts half as much, because players
  change. The model fits on the most recent 24,000 decisions (about 800 games).
* **Learning curve.** Every build refits on 10, 25, 50, 100… of the studied games and tests each fit on the
  same held-out games. The chart on the Twin's page shows how faithfulness grows with data, and where it
  levels off for that player.
* **What more data buys (measured).** For the "attacker" persona, studying 150 games instead of 30
  recovered its king-hunting weight at 1.93 instead of 1.08 (the true value is 2.5), with check at 1.47
  (true 1.5). So its habits came out much more accurately. Its move match on held-out games levelled off
  early: 64% top-3 against Stockfish's 60%, already reached after about 25 games. In other words, more
  games sharpen *how sure* the Twin is about each habit and fill in far more of their opening book. That is
  where a real player's whole career pays off most. More games don't push the single-move match up forever.

## New in v0.9: Opponent Twin (practise against a bot that plays like them)

Prepare → Opponent Library → pick a player → **Play their Twin**.

**How it picks a move**
1. **Their own book.** If they've reached the position before (any move order), it replays what *they*
   chose, weighting recent games more. It trusts the book in proportion to the evidence: one past game is
   followed 40% of the time, five games 83%. **Rehearse their main lines** makes it always play their
   most common move while in book.
2. **Their decision model.** Out of book, Stockfish lists the top 10 moves plus 2 random legal moves (humans
   do play bad moves). Each candidate gets features: expected-score loss (separately for opening,
   middlegame, endgame and time trouble), engine rank, capture, even trade, recapture, check, castling,
   pawn push, developing move, moving an attacked piece away, forward and king-hunting piece moves, early
   queen moves, moving the same piece twice, king walks. A **conditional-logit choice model fitted to
   this player's own decisions** (maximum likelihood, Newton's method with a line search, and a rating-based
   prior so small samples stay sensible) turns those into probabilities, and the Twin samples from them.
   That reproduces how often they err, in which phase, and what they prefer when moves are close.

**How it's built.** A background job replays up to 40 of their games. At each of their moves it scores
the candidates with Stockfish (depth 9) and records what they played: about 30 decisions per game,
1,000+ in total. It fits on 80% of the games and tests on the other 20%. The **faithfulness** score
compares how often their real move is among the Twin's top-3 (and top-1) choices with the same for
Stockfish. Then it refits on everything. The profile shows:
- accuracy by phase, blunders and mistakes per 100 moves, and time-trouble accuracy;
- style traits, shown only when the data supports them (at least 3 standard errors from the prior);
- castling habits and their openings.

**Playing.** You pick a colour and the Twin's strength: "Exactly like them", or "On a good day" (fewer
mistakes, same openings and style). After every Twin move the panel shows why it chose it ("their book: 7
of 9 games…" or "their model: 34% likely…") and what else it considered. Take back, resign and a new game
are available. **Review the game** gives your accuracy against the Twin's, your costliest moves on the board,
and the move where the game left their own games. Games are kept in your browser, so you can continue
later.

**Validation** (`python scripts/twin_validate.py`). This is a parameter-recovery test. Personas with
known habits play 30 games against Stockfish, and a Twin is built from those games alone:

| Persona | Habit recovered | Their move in Twin's top 1 / top 3 | Stockfish top 1 / top 3 |
|---|---|---|---|
| Trader (loves even trades) | trade weight 1.39 (set 2.0), capture 1.13 (set 1.0) | 40% / 68% | 32% / 65% |
| Attacker (checks, goes for the king) | check 1.52 (set 1.5), king-hunt 1.08 (set 2.5) | 39% / 65% | 34% / 56% |
| Weak in endgames | endgame accuracy weight 0.1 vs middlegame 1.9 | 26% / 60% | 27% / 57% |

This tests the fitting machinery. On a real opponent, the faithfulness score shown in the app is the
honest number.

**Limits and safety.**
- Building needs 10+ games, and you can build from chosen time controls only, since openings differ by speed.
- Pro gets 10 builds a day and Coach 30. Once every game is studied (or the plan's study limit is reached)
  a build is "up to date" and costs nothing. A Twin of a public player, and its studied games, are shared by
  everyone who saved that player.
- Moves and reviews are rate-limited, and the `twin` kill switch pauses everything.
- It's a practice bot inside the app, built from public games. It never runs during live games.
- Settings: `TWIN_GAMES` (150 per build since v1.0), `TWIN_DEPTH` (9).

## New in v0.8: Opponent Library (your own Mega Database of opponents)

Prepare → **Opponent Library**. Every player you scout is saved with their games, like a ChessBase database:

* **Search once, open instantly.** Opening a saved player is free and instant. **Update** downloads only games
  played since the last download, and asking for more games (100 → 500) fetches the older ones. A player
  someone downloaded in the last 30 minutes isn't downloaded again. Only downloads count toward the daily
  scout limit, and if you're out of downloads, saved players still open.
* **Shared cache, private notes.** Public Lichess/Chess.com games are stored once for everyone, which means
  fewer requests to those sites. Your list, notes and tags stay private. PGN uploads (over-the-board
  opponents) are private to you and deleted with your account.
* **Explorer (position search).** Tick several players and play moves on the board, or paste a FEN. You
  see every saved game that reached the position, **by any move order**, with each next move's games,
  White/Draw/Black split and the players' score. Click a row to follow it; click a game to replay it.
  Every position up to move 40 is indexed by its Zobrist hash (the key opening books use).
* **Games.** Filter by colour, result, time control, opening, opponent and date. Open any game on the board,
  copy its PGN, or **export the selection as one PGN for ChessBase**.
* **Each player's page.** Scouting report, notes, tags (e.g. "club", "round 3"), Update and Remove.
* **Limits.** Pro saves 150 players, Coach 500 (`library_players` in `app/plans.py`). Free has no library.
* **Size.** About 6.5 KB per game with its position index: 20,000 games is about 130 MB. On that size a
  position search takes 10–130 ms.

The Library and Scout don't need your own chess account linked.

## New in v0.7: ready for real users

* **Security.** Writes from other websites are refused (Origin / Sec-Fetch-Site check on top of SameSite
  cookies). Rate limits are counted in the database, so they hold across several web processes and restarts.
  SSH is keys-only and fail2ban is on after `deploy/setup-vps.sh`.
* **Kill switches.** Pause the AI coach, imports, Chess.com, sign-ups or email in seconds, without a redeploy:
  `python scripts/admin.py switch ai_coach off`, or tick the boxes under Account (admins). The status page
  shows what's paused.
* **AI cost and safety.** Each plan has a daily AI token budget (`ai_tokens_per_day` in `app/plans.py`);
  past it, the built-in coach answers. The prompt treats names, PGN headers and comments as data, never as
  instructions.
* **Audit log.** Plan requests and grants, roster changes, invites, squad joins, account deletions, data
  exports, switch changes and test emails: `python scripts/admin.py audit`.
* **Privacy and legal.** `/privacy`, `/terms` and `/refunds` (drafts: have a lawyer review, then set
  `LEGAL_REVIEWED=1`). Sign-up asks people to confirm they're 18+ and agree; coaches confirm each
  student (or a parent) agreed. **Account → Download my data** gives a zip of everything we hold.
* **Observability.** Every response has an `X-Request-ID`; each request writes one JSON log line with it, and
  error messages quote it, so a user's error reference leads straight to the log line and Sentry event.
* **Release safety.** GitHub Actions runs lint, the tests, `pip-audit` and a Docker build on every push;
  Dependabot proposes updates; the image installs exact, hash-checked versions from `requirements.lock`.
  `RUNBOOK.md` covers incidents, rollbacks, restores and recovery targets; `deploy/loadtest/` has a Locust test.

## New in v0.6

* **Train like a puzzle site.** Your opponent's move plays in first. Then drag or click your answer, with
  legal-move dots, the right promotion piece, sounds and ✓/✗ badges. Get it right and the board continues
  along the engine's line. Get it wrong and you can retry; retries don't change when the puzzle comes back.
  There are two hint levels, *Show solution*, and *Explore*, which lets you play on from any position with
  an eval bar and the engine's best line.
* **A real board everywhere.** Chessnut pieces, four colour themes (Green, Wood, Ice, Slate), animated moves,
  check highlights, and right-click arrows and circles.
* **200–500 games per sync.** Free covers up to 200 games, Pro up to 500. Pick 50, 100, 200, 300 or 500 when
  you sync. Results appear after the first 20 games and refresh every 100. 500 games take about 15 minutes.
* **Coach plan.** The Students page gives a squad heatmap of every student's Rating DNA, each student's
  detail, homework, notes and invite links. Students who join get Pro free and see the homework on their
  Train page. To turn it on for yourself: `python scripts/admin.py grant you@example.com --plan coach`.

## New in v0.5

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

| | Free | Pro (and Event Pass) | Coach |
|---|---|---|---|
| Games per sync | 200 | 500 | 500 |
| Linked accounts | 2 | 6 | 6 |
| Coach questions / day | 15 | 200 | 200 |
| Rating DNA, puzzles, plan | ✓ | ✓ | ✓ |
| ChessBase / PGN upload, OKF bundle export | ✓ | ✓ | ✓ |
| Predicted puzzles / day (your Twin's games) | 8 (60) | 40 (300) | 60 (500) |
| "Think here" games scanned | 100 | 500 | 500 |
| Scoresheet photos / day | 3 | 30 | 100 |
| Report cards / day (written by the AI) | 10 (3) | 100 (30) | 200 (60) |
| Opponent Scout downloads / day | – | 25 | 25 |
| Opponent Library players | – | 150 | 500 |
| Opponent Twin builds / day (games studied) | – | 10 (600) | 30 (2,000) |
| Full-history games per library player | – | 3,000 | 10,000 |
| Prep Check, Repertoire Leaks, Progress, Auto-sync | – | ✓ | ✓ |
| Students dashboard | – | – | up to 20 |

Limits live in `app/plans.py`. Payments aren't wired up: users press **Request Pro**, you see requests with `python scripts/admin.py requests` and grant with `... grant EMAIL`. See `DEPLOY.md` for adding Stripe or Razorpay.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `STOCKFISH_PATH` | auto-detected | Path to Stockfish |
| `ENGINE_DEPTH` / `ENGINE_TIME` | `12` / `0.08` | Search limit per position |
| `MAX_GAMES` | `500` | Server-wide cap on games per sync (cost control) |
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
| `REQUIRE_TERMS` | `1` | New accounts must confirm they're 18+ and accept the Terms and Privacy Policy |
| `LEGAL_ENTITY` / `LEGAL_CITY` / `LEGAL_UPDATED` / `LEGAL_REVIEWED` | unset | Who runs the service, courts, date and draft notice on `/privacy`, `/terms`, `/refunds` |
| `TWIN_GAMES` / `TWIN_DEPTH` | `150` / `9` | Games an Opponent Twin studies per build, and the engine depth it uses |
| `LIBRARY_FULL_MAX` | `10000` | Server cap on games kept per library player (full-history downloads) |
| `VISION_PROVIDER` / `VISION_MODEL` / `VISION_BASE_URL` / `VISION_API_KEY` | `auto` / unset | Which model reads scoresheet photos (`off` to disable; see v1.1) |
| `VISION2_MODEL` / `VISION2_BASE_URL` / `VISION2_API_KEY` | unset | A second photo reader (any OpenAI-compatible vision model); readings are merged by vote (see v1.2) |
| `REVIEW_DEPTH` | `14` | Engine depth for game report cards (both players, top two moves per position) |
| `SCANS_PER_HOUR` | `20` | Scoresheet photos per user per hour (on top of the plan's daily allowance) |
| `BANK_DEPTH` / `PREDICT_POOL` | `10` / `300` | Engine depth for annotating bank puzzles; puzzles kept ready around each player's level |
| `SELF_TWIN_MIN_GAMES` | `10` | Games your own Twin needs before it's used for predictions |
| `THINK_DEPTH` | `10` | Engine depth for finding critical moments in your games |
| `LOG_LEVEL` | `INFO` | App log level; the JSON access log is the `plateau.access` logger |
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
| `scripts/admin.py` | Users, upgrade requests, grant/revoke plans, kill switches, audit log, email checks |
| `app/twin.py` | Opponent Twin: features, choice-model fitting, book, building, move choice |
| `app/library.py` | Opponent Library: saved players, incremental updates, position index and search, PGN export |
| `app/switches.py` | Kill switches stored in the database, read by every process |
| `app/legal.py` | Privacy policy, terms and refund policy pages |
| `app/puzzlebank.py` / `scripts/import_puzzles.py` | The Lichess puzzle bank: balanced import, shared Stockfish annotation |
| `app/predict.py` | Your Twin from your own games, puzzle rating (Glicko-1), theme skills, miss prediction, the predicted deck |
| `app/think.py` | Time per move from clocks, critical moments, the cost of rushing, the "Think here" drill |
| `app/scoresheet.py` | Notation in seven languages, handwriting-aware matching, legal-move beam search, vision models (several readers merged by vote, a multiple-choice second look, a human-move prior), PGN |
| `app/review.py` / `app/review_ai.py` | Game report cards: move classes, grades, key moments, annotated PGN; the coach's grounded write-up |
| `app/openings.py` / `app/data/openings.tsv` | Opening names and ECO codes by position (Lichess's open list, CC0) |
| `app/static/js/sheetcv.js` | Photo cleanup in the browser: sheet corners, perspective flattening, shadow removal, column crops |
| `scripts/make_puzzle_sample.py` | Generates the bundled sample puzzles with Stockfish (Lichess-puzzler style) |
| `RUNBOOK.md` | Incidents, kill switches, restores, releases and rollbacks |

## Tests

```bash
pip install -r requirements-dev.txt
pytest        # 360 tests; engine tests skip if Stockfish is missing
ruff check .
```

The demo data (`sample_data/demo_games.pgn`) is 40 synthetic games for `demo_player`. Use real games to judge the product.

## Third-party files

chess.js (BSD-2-Clause, `app/static/vendor/`), the Chessnut piece set by Alexis Luengas (Apache-2.0,
`app/static/pieces/chessnut/`, with its licence) and Lichess's opening list and puzzle database (CC0).
