# Plateau Breaker runbook

What to do when something breaks, how to release safely, and what we promise about data. Keep this short
and current. Every command runs from `~/chess_analytics/deploy` on the server, as the `plateau` user.

```bash
alias pb='docker compose exec web python scripts/admin.py'    # pb switch, pb audit, pb jobs, pb email ...
```

## Recovery targets

| | Target | How |
|---|---|---|
| **RPO** (data we can lose) | 24 hours | Encrypted daily backup to object storage (`BACKUP_HOUR_UTC`), restore-tested weekly by the scheduler |
| **RTO** (time to be back up) | 1 hour | New VPS + `setup-vps.sh` + `docker compose up` + restore (section 4) |
| Uptime goal | 99.5% a month (about 3.5 hours down) | Uptime monitor on `/api/health`, status page at `/status` |

Tighten the RPO later by running `python scripts/backup.py run` every few hours from cron, or by moving to
Postgres with point-in-time recovery.

## 1. First five minutes of any incident

1. Open `https://YOUR_DOMAIN/status`. It tells you which part is broken (database, Stockfish, imports, AI
   coach, backups, email, Lichess, Chess.com) and what is paused.
2. Look at the logs: `docker compose logs --since 15m web worker | tail -200`. Every request has one JSON
   line with a request ID (`rid`). A user who quotes an error reference gives you the `rid`:
   `docker compose logs web | grep RID`.
3. Check Sentry for the same `request_id` tag.
4. If one feature is failing, **switch it off** (section 2) rather than letting users hit errors. Then fix.
5. Post a short note to users if it lasts more than 15 minutes (email `SUPPORT_EMAIL` replies, social).

## 2. Kill switches (no redeploy)

| Switch | When to use it | What users see |
|---|---|---|
| `ai_coach` | Model provider down, slow, or spend running away | The built-in coach answers, with a notice |
| `imports` | Queue backed up, CPU pinned, a bad import bug | "Syncing is paused for maintenance. Your games are safe." |
| `chesscom` | Chess.com rate-limiting or blocking us | Chess.com sync paused; Lichess and PGN still work |
| `signups` | Spam signups, or a launch spike you can't handle | "New accounts are paused for a short while." |
| `emails` | Provider failing or sending to the wrong place | Emails are saved to `outbox/` instead of sent |
| `twin` | Workers needed for imports; Twin builds misbehaving | Twin practice and builds paused; library and reports work |
| `predicted` | Puzzle annotation hogging workers | No new predicted puzzles; the existing deck works |
| `think` | "Think here" scans hogging workers | No new scans; the drill works |
| `scanner` | Vision provider failing, or its bill spiking | Photo reading paused; typing the moves works |
| `review` | Report cards hogging workers, or a bug in them | No new report cards; existing ones still open |

```bash
pb switch                     # show all
pb switch ai_coach off        # pause
pb switch ai_coach on         # resume
```

Or tick the boxes under **Account → Service switches** when signed in as an admin. Every change is in the
audit log (`pb audit`) and shows on the status page within 5 seconds.

## 3. Common incidents

| Symptom | Likely cause | Fix |
|---|---|---|
| Coach answers say "built-in coach is answering" | Model provider errors or rate limits | `docker compose exec web python scripts/coach_check.py "test"` shows the provider's reply. Add billing on Groq or set `LLM_FALLBACK_*`. |
| Imports waiting a long time | Big launch-day backlog | `pb jobs`. Add workers: `docker compose up -d --scale worker=2`, or a bigger server. Pause `imports` if CPU is pinned. |
| "database is locked" in logs | Too many writers for SQLite | Short term: fewer workers. Long term: Postgres (see README "Scaling"). |
| No emails arriving | Provider settings or quota | `pb email` then `pb test-email you@example.com` print the exact problem. |
| Disk full | Logs, backups or `outbox/` | `df -h`, `docker system prune`, check `BACKUP_KEEP`. |
| Site down, container restarting | Crash at start | `docker compose logs web | head -50`; roll back (section 5). |
| Suspicious admin activity | Leaked admin password | `pb audit 200`, change the password, rotate `LLM_API_KEY` and email keys, check `ADMIN_EMAILS`. |

## 4. Restore from a backup

```bash
docker compose exec scheduler python scripts/backup.py list                 # newest last
docker compose run --rm web python scripts/backup.py restore --to /data/restored.db   # add --name NAME for an older one
docker compose stop web worker scheduler
docker compose run --rm web sh -c 'mv /data/plateau.db /data/plateau.db.broken && rm -f /data/plateau.db-wal /data/plateau.db-shm && mv /data/restored.db /data/plateau.db'
docker compose up -d
```

The scheduler runs this restore against a scratch file every week and reports on the status page, so
you know the backups actually work before you need them.

## 5. Releasing (and rolling back)

1. Every change goes through a pull request; CI (tests, lint, `pip-audit`, image build) must be green.
2. Tag the release: `git tag v0.7.0 && git push --tags`.
3. **Staging first:** a second small VPS (or a second compose project on the same one with its own
   `DOMAIN` and `DB_PATH`) running the same tag, with a copy of last night's production backup restored
   into it. Click through sign-in, sync, Train, coach and the Students page.
4. Production: `git fetch --tags && git checkout v0.7.0 && docker compose up -d --build`.
   Database changes are additive (new tables and columns only) and run on start-up; the backup from
   step 3 proves they work on real data.
5. **Rollback:** `git checkout v0.6.6 && docker compose up -d --build`. Additive schema changes mean the old
   version runs fine on the newer database. If data itself is damaged, restore (section 4).

## 6. Secrets

* Secrets live only in `deploy/.env` (`chmod 600`, never committed) or Codespaces secrets for development.
* Use different keys for development and production.
* If a key is ever pasted somewhere public (chat, an issue, a screenshot), rotate it at the provider at
  once, update `.env`, and run `docker compose up -d`.

## 7. Capacity planning

* One import worker analyses about 2,000 games an hour (depth 12). A user's first sync is up to 200 games
  (Free) or 500 (Pro).
* Groq's free tier allows about one coach question a minute across all users; the paid tier removes that.
  Each user also has a daily AI token budget (`ai_tokens_per_day` in `app/plans.py`), so one heavy user
  can't run up the bill.
* Opponent Library and Twins: about 6.5 KB per saved game (with the position index) and 2 KB per studied
  game. 100 players with full histories of 3,000 games is about 2 GB. A Twin build studies about 1 game a
  second per worker. Pause them with `pb switch twin off` if workers are needed for imports.
* Predicted puzzles: each bank puzzle is annotated once for everyone (about 0.1 s at depth 10), so the first
  users at a new level wait a minute while 300 puzzles are prepared. Pre-annotate with
  `python scripts/import_puzzles.py --annotate 5000 --around 1700`. Your-own-Twin builds and "Think here"
  scans cost about 1 second per game. Pause them with `pb switch predicted off` / `pb switch think off`.
* Scoresheet photos call the vision model (about 2,000 tokens each, plus a short follow-up about unclear
  moves; double that with a second reader, `VISION2_*`). If the provider fails or bills spike:
  `pb switch scanner off`. Typing moves keeps working.
* Game report cards take 10–20 seconds of one worker each at `REVIEW_DEPTH=14` (Free users get 10 a day).
  The AI write-up is one model call of about 3,000 tokens. If workers fall behind: lower `REVIEW_DEPTH` to 12,
  or `pb switch review off`. With `pb switch ai_coach off`, reviews are written by the built-in coach.
* Load test before a launch: `deploy/loadtest/locustfile.py` (instructions inside). Watch the status page's
  queue wait, p95 latency in the access log, and the error rate.
