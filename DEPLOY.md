# Taking Plateau Breaker to production

This guide puts the app on one small VPS with HTTPS, a job queue worker, daily backups,
email, error monitoring, analytics, a status page and a support address. Plan on about an hour.

```
                internet
                   │  443 (HTTPS, automatic certificate)
              ┌────▼────┐
              │  Caddy  │
              └────┬────┘
                   │ :8000
              ┌────▼────┐   queues work    ┌──────────┐   Stockfish
              │   web   │ ───────────────► │  worker  │ ◄──────────
              └────┬────┘    (SQLite)      └──────────┘
                   │                       ┌───────────┐  daily backup → S3/R2/B2
                   └──── /data volume ─────│ scheduler │  weekly restore test
                                           └───────────┘  weekly digest email
```

## 0. Before you charge anyone: data-source permissions

| Source | What their terms say | What to do |
|---|---|---|
| **Chess.com** PubAPI | Free community tools are welcome. **Selling a tool built on the API needs a proposal to their Business Development team.** | Email them before launch. Until they agree, keep `CHESSCOM_ENABLED=0` (Lichess + PGN only). |
| **Lichess** | Commercial use is allowed within reasonable limits, at Lichess's discretion. | The app already sends one request at a time and pauses a minute after a 429. Set `CONTACT_EMAIL`. |

Publish a Terms of Service, Privacy Policy and Refund Policy before taking payments. Users can delete
their account and all its data from **Account → Delete account**. India's DPDP rules treat under-18s as
children who need verified parental consent, so launch as 18+.

## 1. What you need

| Thing | Needed? | Examples |
|---|---|---|
| A VPS | Yes | Hetzner, DigitalOcean, Vultr, AWS Lightsail. Ubuntu 24.04, **2–4 vCPU, 4–8 GB RAM**. Each Stockfish process holds ~100–250 MB; the default setup runs four. |
| A domain | Yes | Point an `A` record (and `AAAA` for IPv6) at the VPS. |
| Email sending | Strongly recommended | Brevo or Resend (HTTPS API), or any SMTP provider: Postmark, Amazon SES, Zoho Mail. Without it, verification and reset emails are only saved on the server. |
| Object storage for backups | Strongly recommended | Cloudflare R2, Backblaze B2, AWS S3, Hetzner Object Storage. |
| A paid model plan and/or a second provider | Recommended | Groq's paid tier; OpenRouter, Gemini, Together or Cerebras as the fallback. |
| Sentry | Optional | Free developer plan is enough to start. |
| Plausible or PostHog | Optional | Privacy-friendly analytics. |

## 2. Server setup (once)

```bash
ssh root@YOUR_SERVER_IP
curl -fsSL https://raw.githubusercontent.com/<you>/chess_analytics/main/deploy/setup-vps.sh -o setup-vps.sh
bash setup-vps.sh
```

The script installs Docker, opens only ports 22/80/443, adds 2 GB of swap, turns on automatic security
updates and fail2ban, and creates a `plateau` user. Then:

```bash
su - plateau
git clone https://github.com/<you>/chess_analytics.git
cd chess_analytics/deploy
cp .env.example .env
nano .env            # at least DOMAIN, PUBLIC_URL, ADMIN_EMAILS, SUPPORT_EMAIL, LLM_* keys
                     # put values containing $, spaces or # in single quotes
docker compose up -d --build
docker compose logs -f web worker
```

Open `https://YOUR_DOMAIN`. Caddy fetches the certificate on the first request, which can take up to a minute.
Sign up with the email in `ADMIN_EMAILS` to get Pro and admin rights, then open `https://YOUR_DOMAIN/status`.

`COOKIE_SECURE=1` (already in `.env.example` and the Docker image) means sign-in cookies are only sent
over HTTPS. Don't turn it off in production.

## 3. Email: verification, password reset, support, weekly digest

Pick one way to send:

| Option | Settings | When |
|---|---|---|
| **Brevo API** (recommended) | `BREVO_API_KEY`, `EMAIL_FROM` | You have a domain. Free plan: 300 emails/day. Sends over HTTPS (port 443), so it works even where the host blocks mail ports, as several cloud hosts do for new accounts. |
| **Resend API** | `RESEND_API_KEY`, `EMAIL_FROM` | Same idea; needs a verified domain before it sends to other people. |
| **SMTP** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Any provider (Postmark, SES, Zoho, Brevo SMTP), or Gmail with an App Password while you're small. |

1. **Verify your sending domain** with the provider and add the **SPF**, **DKIM** and **DMARC** DNS records
   it gives you. `EMAIL_FROM` must be an address at that domain, e.g. `hello@yourdomain.com`. Don't send
   "from" a gmail.com address through Brevo or Resend: it can't be authenticated, and Gmail and Yahoo may
   silently drop it.
2. Brevo: create an **API key** (SMTP & API → API Keys). If Brevo's **Authorised IPs** check is on, add the
   server's IP address (Security → Authorised IPs), or the API refuses it.
3. Set `SUPPORT_EMAIL` (where support messages go) and `PUBLIC_URL` (links in emails use it).
4. Apply and test:

```bash
docker compose up -d
docker compose exec web python scripts/admin.py test-email you@example.com
docker compose exec web python scripts/admin.py email      # the current setup and any warnings
```

If the test fails, the message says what to change (wrong password, port and TLS mismatch, blocked port,
unverified sender, ...). The same test is a button in **Account → Email sending** for `ADMIN_EMAILS` users.

The status page's **Email** row reflects what actually happened: it turns red when the last email failed to
send, and green again after the next one goes out. A single mistyped recipient address doesn't count.

Support messages are also stored in the database: `docker compose exec web python scripts/admin.py support`.
`REQUIRE_EMAIL_VERIFICATION=1` blocks syncing and the coach until an email is confirmed (it reduces fake
signups; leave it off while you're small).

## 4. Backups (daily, tested weekly)

1. Create a **private** bucket, e.g. `plateau-backups`, and an access key that can only use that bucket.
2. In `.env`: `BACKUP_TARGET=s3://plateau-backups/daily`, `S3_ENDPOINT_URL` (for R2:
   `https://<accountid>.r2.cloudflarestorage.com`; for B2: `https://s3.<region>.backblazeb2.com`; empty
   for AWS), `S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
3. Optional but recommended: `BACKUP_ENCRYPTION_KEY` (a long passphrase). Backups are then encrypted
   before upload. **Store the passphrase in your password manager**: without it, backups can't be restored.
4. Test now, then let the scheduler take over (daily at `BACKUP_HOUR_UTC`, restore test every Sunday):

```bash
docker compose up -d
docker compose exec scheduler python scripts/backup.py run
docker compose exec scheduler python scripts/backup.py test-restore
docker compose exec scheduler python scripts/backup.py list
```

The status page shows the age of the last backup and the last restore test.

**Restoring after a disaster:**

```bash
docker compose stop web worker scheduler
docker compose run --rm scheduler python scripts/backup.py restore --to /data/restored.db
docker compose run --rm scheduler sh -c "mv /data/plateau.db /data/plateau.db.broken && mv /data/restored.db /data/plateau.db && rm -f /data/plateau.db-wal /data/plateau.db-shm && rm -rf /data/bundles"
docker compose up -d
```

The database is the only thing to restore: deleting `/data/bundles` makes every knowledge bundle rebuild
from the restored games the first time it's opened.

## 5. The AI coach: never fail on a free-tier limit

* **Paid tier:** in the Groq console, add billing to move off the free tier's per-minute and daily token limits.
* **Second provider:** set `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL`. When the
  first model errors, times out or hits a limit, the coach asks the second one. Only if both fail does
  it fall back to the built-in coach. Answers still go through the same grounding check either way.
* The status page's **AI coach** row turns red if recent answers keep falling back.

## 6. Monitoring

* **Errors:** create a Sentry project (platform: Python/FastAPI), set `SENTRY_DSN`. The web app, worker and
  scheduler report crashes; browser errors are relayed through `/api/client-error`, so no Sentry script
  runs in visitors' browsers. Cookies, request bodies and email addresses are scrubbed before sending.
* **Uptime:** point an uptime monitor (UptimeRobot, Better Stack) at `https://YOUR_DOMAIN/api/health`.
* **Status page:** `https://YOUR_DOMAIN/status` checks the database, Stockfish, the import queue, the AI
  coach, backups, email, Lichess and Chess.com, and links to support. Share it when something breaks.
* **Queue:** `docker compose exec web python scripts/admin.py jobs`.

## 7. Analytics (privacy-friendly)

* **Plausible:** add your site in Plausible, then `ANALYTICS=plausible` and `PLAUSIBLE_DOMAIN=YOUR_DOMAIN`.
* **PostHog:** `ANALYTICS=posthog`, `POSTHOG_KEY`, `POSTHOG_HOST`. Session recording and autocapture are off.

Events sent: Preview started/done, Signup, Login, Account linked, Sync started, Coach question, Puzzle
attempt, Upgrade request, Scout report, DNA shared. Never emails or usernames. The page's Content
Security Policy only allows the analytics host you configured.

## 8. Capacity

* `MAX_CONCURRENT_IMPORTS` = worker threads (about one per 2 cores). Imports run in chunks of
  `IMPORT_CHUNK` games and a user never has two jobs running, so one big import can't block others.
* More capacity: a bigger server, or more worker processes: `docker compose up -d --scale worker=2`.
* The worker runs at lower CPU priority (`nice 10`), so the site stays responsive while it analyses.
* Cap free usage in `app/plans.py` (games per sync, coach questions per day).

## 9. Updating

```bash
cd ~/chess_analytics && git pull
cd deploy && docker compose up -d --build
```

Running jobs are re-queued automatically if a worker restarts mid-import.

## 10. Payments (next step)

Plans are enforced server-side (`app/plans.py`). The Plans page records requests; grant them with
`docker compose exec web python scripts/admin.py grant EMAIL` (`--days 10` for an Event Pass). To take
money: Razorpay Subscriptions for India, a merchant of record (Dodo Payments, Paddle) abroad. Add a
checkout route and a signature-verified webhook that calls `store.set_plan(user_id, "pro")`, or
`set_plan(user_id, "pro", expires_at)` for an Event Pass.

## Without Docker

`deploy/systemd/` has units for the web app, worker and scheduler. Install Python 3.12, Stockfish
(`apt install stockfish`) and Caddy from apt, create a virtualenv in `/opt/plateau/.venv`, run
`pip install -r requirements-prod.txt`, put the settings in `/etc/plateau.env`, and point Caddy at
`127.0.0.1:8000`.
