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
