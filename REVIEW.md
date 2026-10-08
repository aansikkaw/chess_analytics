# Code review: Plateau Breaker v0.1

**Verdict:** works end to end and is ready as a local, single-user prototype. It is **not** ready to host publicly: there is no login, and nothing stops a stranger from running up your API bill. Both are listed below with the fix.

## What was checked

| Check | Result |
|---|---|
| Unit + integration tests (`pytest`) | 29 passing: maths, PGN parsing, engine analysis on known positions, storage and spaced repetition, the agent loop (with a fake LLM client), and the full API flow |
| Lint (`ruff`: errors, bugbear, pyupgrade, simplify) | Clean |
| Live server smoke test | Demo import, profile, puzzles, plan, coach: all OK, no server errors |
| Browser test (headless Chromium, desktop 1200px and phone 390px) | All four views render; no JS errors; no sideways scrolling on phone |
| Manual read of every module | Findings below |

**Not tested:** the AI-agent coach against the real Anthropic API (no key in the build environment). The loop logic is tested with a scripted fake client, but run a few real questions before you trust the wording.

---

## Bugs found and fixed during review

Each one is a common class of mistake, so it's worth knowing why it happened.

**1. Decided positions were hiding weaknesses (methodology).**
The demo player has a planted time-trouble weakness, yet the first version rated its time management *above* average. The cause: once a game is +8, almost any move keeps the win probability near 100%, so it scores ~100% accuracy. Late-game, low-clock moves are often in decided positions, which inflated exactly the skills we wanted to catch. Fix: skill scores only use "contested" moves (within ±6 pawns). After the fix, blunders under time pressure came out at 2.3× the normal rate.
*Lesson:* when a metric saturates, averaging over the saturated region dilutes the signal. Always sanity-check a model against a case where you know the answer.

**2. Tabs didn't hide anything (CSS).**
Every view showed at once. `.view { display: grid }` overrides the browser's default `[hidden] { display: none }`, because an author rule beats the browser's own stylesheet. Fix: `[hidden] { display: none !important }`.

**3. Page scrolled sideways on phones.**
A CSS grid child defaults to `min-width: auto`, so the wide tab bar forced the whole column wider than the screen. Fix: `.wrap > * { min-width: 0 }`.

**4. Recaptures counted as "tactics".**
`_is_tactical` checks the previous move to exclude plain recaptures, but the stored boards were copied with `copy(stack=False)`, which drops move history, so the check never fired. Fix: keep the move stack. Test: `test_plain_recapture_is_not_a_tactic`.

**5. The coach stated tiny samples as fact.**
With 5 games it said "under time pressure your play rates about 1942" from 4 moves. Fix: skills with fewer than 25 moves are flagged low-confidence in the UI (faded) and in coach replies.

**6. Memory growth in a long-running server.** The in-memory job list and the engine's position cache grew forever. Both are now bounded.

**7. Small consistency fixes.** The plan endpoint now accepts the same `?rating=` override as the profile. The exception class was renamed from `ImportError_` to `GameImportError`. The offline coach no longer reaches into a private attribute.

**Checked and confirmed correct:** PGN `[%clk]` is the time left *after* a move, so "clock before move N" is read from your previous move. My first test assumed the opposite and was wrong; the code was right.

---

## Open issues, by priority

### Must fix before putting it on the internet
1. **No authentication.** Anyone who knows a username can read that player's data, and anyone can submit puzzle attempts by id. Add login (e.g. Lichess OAuth, which also proves who owns an account) and check ownership on every `/players/{user}` and `/puzzles/{id}` route.
2. **No rate limiting.** Every coach message with an API key costs money, and every import burns CPU on Stockfish. Add per-user limits (e.g. `slowapi`), a daily coach-message budget, and a cap on concurrent imports.
3. **Analysis runs inside the web process.** Fine locally. In production, move imports to a job queue (RQ, Celery or Arq) so a restart doesn't lose running jobs and heavy imports don't slow the API.

### Product quality
4. **Rating DNA isn't calibrated.** "12 rating points per accuracy point" is a reasonable guess, not a measured number. This is a good analytics project in its own right. Take a sample from the free Lichess open database, compute phase-level accuracy per player, and regress it on rating to estimate the real slope. Then publish the method on the page, since credible numbers are part of the product.
5. **Skills overlap.** A single move can count toward endgame, tactics and time pressure at once, so skill ratings aren't independent. Say so in the UI, or switch to a model with all skills fitted jointly.
6. **One-answer puzzles.** Many positions have several good moves. Answers within 3 win-% of the best are accepted ("also good"), but at depth 12 that check can be noisy. Raise the depth for puzzle checks, or pre-compute the acceptable moves at import time.
7. **Time-pressure threshold ignores the increment.** The rule is under 10% of base time (minimum 30s). With +10 increment, 60 seconds isn't really trouble. Scale the threshold by increment.
8. **Engine strength.** Depth 12 / 80ms suits club-level coaching but can misjudge subtle endgames. Expose a "deep analysis" option for single games.

### Low
9. **Prompt-injection surface.** PGN headers (player names, opening names) flow into tool results the LLM reads. Impact is small because every tool is read-only, but treat tool output as data in the system prompt.
10. **Raw error text** from the Anthropic SDK is returned to the browser on coach failures. Log it server-side and show a generic message instead.
11. **Plan checkboxes** are stored by day position, so they can attach to the wrong task after the plan regenerates.
12. **Overall accuracy** is a plain mean over moves. Lichess uses a volatility-weighted per-game mean, so numbers will differ slightly from Lichess's own.

---

## Design notes (what's good, and why)

- **The LLM never invents chess.** The agent has no board knowledge of its own. It must call `list_critical_moments` or `analyse_position`, and the system prompt forbids mentioning lines that didn't come from a tool. This is the main trust problem with AI chess chat, and the architecture addresses it rather than hoping the prompt does.
- **Offline mode is the same tools, minus the LLM.** The app is useful and testable with no API key, and the agent's behaviour can be compared against a deterministic baseline.
- **Pure functions where it matters.** `win_percent`, `move_accuracy`, `classify` and `game_phase` have no side effects, which is why they're trivial to test.
- **Answers never reach the browser early.** The puzzle list strips the solution, and it's returned only after an attempt (tested).
- **Untrusted text is never inserted as HTML** in the front end (`textContent` throughout), so a hostile username or PGN header can't inject script.

---

## Addendum: free LLM providers (v0.2)

- Added `app/llm.py`. The coach now runs on Claude, any OpenAI-compatible API (Groq, Gemini, OpenRouter...) or local Ollama, chosen automatically.
- **Small-model safeguard:** open models of around 3B parameters often skip tool calls and then guess. For OpenAI-compatible providers, the system prompt now carries a compact brief of verified facts (skill ratings, top findings, three costliest moments with engine lines), and tools remain available for detail.
- **Graceful failure:** any provider error (timeout, 404 missing model, 401 bad key, 429 rate limit) returns the offline coach's answer with a one-line reason, never a 500.
- **Tests:** 33 passing. The new ones cover the OpenAI-style tool loop against a mock server, the fallback path, argument parsing, and provider priority. An end-to-end run through the live API against a fake model server also passed.
- **Not verified:** a real Ollama model, which couldn't be downloaded in the build environment. Expect answers from a 3B model to be noticeably weaker than Claude's. Treat them as explanations of your verified data, not independent chess analysis.

---

## v0.3 review: retrieval, accounts, Pro features

**Status:** 70 tests passing, lint clean, and a full browser run (sign-up → link → sync → every tab, desktop and phone) with no JS errors.

**What changed**
- **Direct import** from Lichess and Chess.com, with incremental sync (`since`), time-control choice, and clear errors (unknown user vs closed account vs rate limit).
- **Retrieval + grounding** (`app/rag.py`): BM25 + filters + synonyms over every costly moment, exact position similarity, stable citations, and a verify → rewrite → flag loop on every LLM answer.
- **Accounts:** scrypt password hashes, hashed session tokens, per-IP login throttling. Every data route checks ownership, and two users linking the same handle get separate data.
- **Plans:** Free/Pro feature gates and daily limits enforced server-side; upgrade requests plus an admin CLI.
- **Pro:** Opponent Scout, Repertoire Leaks + recurring mistakes, Progress, Auto-sync.
- **Ops:** Dockerfile (non-root, persistent `/data`), deploy guide, security headers.

**Found and fixed during this review**
1. *The server-wide game cap had stopped applying* after the rewrite (only the plan limit capped imports). That cap is your CPU-cost guard. Restored as `min(plan limit, MAX_GAMES)` for manual and auto-sync.
2. *Parallel requests to Lichess/Chess.com.* Both platforms ask for serial access. Two users syncing at once would have sent parallel requests. Now each platform has a lock, and after any 429 that platform is paused for 60 s for everyone (auto-sync skips it for the round).
3. *Repertoire looked empty on varied histories*, because the tree started at 4 plies. It now includes 2-ply lines.
4. *Citation chips repeated the move text* already in the sentence. Now a compact "♟ view" button with a descriptive label.

**Verified behaviour worth knowing**
- A simulated model that answers "Qh5 wins, +4.7" (not in the data) is rejected and rewritten using the cited moment's real moves (tested in code and in the browser).
- If the rewrite still contains unverified moves, the answer is shown with an explicit ⚠ listing them (tested).

**Limits of the grounding check (be honest about these in marketing)**
- It verifies that moves and evaluations *appear in retrieved data*, not that the model's *explanation* of them is correct. A model can still mis-describe why a verified move is good.
- Bare squares ("the e4 pawn") and moves written without a number ("then d5") aren't checked, to avoid flagging normal prose.
- So "verified" means *every concrete move and number is real*, not *every sentence is true*.

**Open items before charging money** (details in DEPLOY.md)
1. **Chess.com commercial approval.** Their docs require it for commercial apps. `CHESSCOM_ENABLED=0` lets you launch on Lichess while you apply.
2. Payments (Stripe or Razorpay checkout + webhook), password reset, account deletion, ToS and Privacy pages.
3. **Single-process design.** Jobs and auto-sync live in memory/thread, so run one instance. For multiple instances, move jobs to a queue (RQ/Celery/Arq) and the database to Postgres.
4. **Rating DNA calibration** (unchanged from v0.1): the 12-points-per-accuracy-point scale is still a heuristic.
5. Scout fetches up to 100 opponent games synchronously (10–30 s). Fine at small scale; make it a background job if it becomes popular.
6. The login throttle is per-process and in-memory. Behind a proxy it relies on `--proxy-headers` (set in the Dockerfile).

---

## v0.4 review: OKF knowledge base and ChessBase Prep Check

**Status:** 99 tests passing, lint clean, full browser run (ChessBase upload, Prep Check, drills, Knowledge tab, concept links in coach answers; desktop and phone) with no JS errors. Both bundles also pass **Google's reference OKF parser** (`GoogleCloudPlatform/knowledge-catalog`), whose trust tiers matched ours (engine concepts machine-confirmed, principles unverified).

**Design decision: OKF is the knowledge layer, not a replacement for search.** The OKF spec says agents navigate bundles by reading `index.md` and following links, and it explicitly leaves room for retrieval alongside. So the old ad-hoc moment index is gone. Knowledge lives in OKF concepts, and the coach navigates them (`read_index` → `read_concept` → links). Keyword search and frontmatter filters remain as *tools over the bundle*.

**What this bought**
- Structure: one concept per file with typed, queryable frontmatter, so every fact the coach cites has an address (`/moments/…`) and a trust tier.
- Explanations are grounded too: patterns and skills link to principle concepts, so the *why* comes from reviewable text rather than the model's memory. This closes the main limit noted in v0.3.
- Portability: users can download their bundle and version it in git.

**Found and fixed during this review**
1. *Real ChessBase names couldn't be entered.* Account names only allowed username characters, so "Sikka, Aanya" was rejected. My first test had bypassed the API and hidden this. PGN accounts now accept real names, and matching handles "Last, First", accents and underscores.
2. *Large databases imported their oldest games first.* Imports are now sorted newest first before the plan cap applies.
3. *Board rows collapsed* on ranks with no pieces (visible on repertoire drills from the opening). Squares now have a fixed aspect ratio.
4. *Existing databases needed new columns* (`puzzles.accept`, `puzzles.kind`). Added an idempotent migration that runs on start; tested against a v0.2-shaped database.
5. A test position I wrote for "prep holes" was illegal chess (`Kxf7` with a bishop on c4). The parser correctly rejected it; the test now uses a real queen-dropping line.

**Limits to be honest about**
- Prep Check matches by position, so transpositions are recognised. A game that starts with a different first move as White is treated as "a different repertoire" and skipped, not counted as a deviation.
- Gap suggestions and hole checks use the app's fast engine settings (depth 12 / 80 ms). Treat them as prompts to investigate in ChessBase, not as final theory.
- The principles bundle is written by the app's author and marked unverified. Review it (you're the 2000-rated expert) and add `verified: {by: human:<you>, at: …}` to each concept you agree with; the app will then show them as human-reviewed.
- Native ChessBase files (`.cbh`, `.cbv`) can't be read; users must export to PGN. The upload explains how.

---

## v0.5 pre-launch review (independent reviewer, then fixed)

The v0.5 changes (new UI, job queue, email, backups, monitoring, deployment) were reviewed by a separate
agent that hadn't written them. Everything it found is fixed and covered by a test where testable:

| Severity | Finding | Fix |
|---|---|---|
| High | Sentry would have received passwords: sentry-sdk sends stack-frame locals by default | `include_local_variables=False`, no request bodies, scrubbed log entries, breadcrumbs and query strings |
| High | Unversioned JS/CSS cached for a week by Caddy: a deploy would break returning users | App sends `no-cache` for code (cheap 304s), long cache only for fonts/icons; service worker bypasses the HTTP cache on install |
| Medium | Deleting an account mid-import left games and a bundle behind | Import checks the account before and after each save and sweeps; queued jobs are cancelled; support messages deleted with the user |
| Medium | Any S3/network error crash-looped the scheduler | Every chore failure is recorded (status page turns red) and reported, never raised |
| Medium | `backup.py restore` failed when the live database was corrupt | Database only opened for `run`/`test-restore`; restore steps also clear bundles |
| Medium | Inline comments in `.env.example` broke systemd; `$` in secrets broke Compose | Comments on their own lines; quoting rule documented |
| Medium | Reset tokens could reach analytics and access logs via the URL | Tokens travel in the `#fragment` (never sent to servers) and are removed from the address bar before analytics loads; no Caddy access log |
| Medium | Incremental sync could skip games played during an import longer than an hour | `last_synced_at` is the time games were fetched, not when analysis finished |
| Low–Medium | No signup rate limit; login timing revealed registered emails | Per-IP signup limit; a scrypt check runs for unknown emails too |
| Low | Two imports could start for one account (double click, auto-sync race) | Atomic one-active-job-per-account enqueue |
| Low | Weekly restore test could fail falsely and wasn't shown | Checks integrity and plausibility; a failure shows on the status page |
| Low | Coach waited out the first provider's rate limit before trying the fallback | Switches immediately when a fallback exists; 60 s timeout for hosted APIs |
| Low | Digest starvation, GET-unsubscribe by link scanners, per-process Lichess lock, job polling read the PGN, previewed username linked on any sign-in | Ordered digest rotation; unsubscribe needs a POST (plus RFC 8058 one-click headers); file lock + shared pause file; payload-free job view; preview links only on new signups |

Known and accepted: signup still says "an account with that email already exists" (most sites do; the
per-IP limit slows enumeration).
