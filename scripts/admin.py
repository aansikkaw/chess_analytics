"""Admin tasks from the command line (run where the database lives).

  python scripts/admin.py users                    list users and plans
  python scripts/admin.py requests                 list upgrade requests (Pro, Event Pass, Coach waitlist)
  python scripts/admin.py grant EMAIL              switch a user to Pro
  python scripts/admin.py grant EMAIL --days 10    Pro for 10 days (an Event Pass); back to Free afterwards
  python scripts/admin.py revoke EMAIL             switch a user back to Free
  python scripts/admin.py support                  list recent support messages
  python scripts/admin.py jobs                     queue status
  python scripts/admin.py email                    how this server sends email, and what's wrong with the settings
  python scripts/admin.py test-email YOU@EXAMPLE   send a test email now; if it fails, says what to change
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import load_settings  # noqa: E402
from app.jobs import JobQueue  # noqa: E402
from app.mailer import MailError, Mailer  # noqa: E402
from app.plans import effective_plan  # noqa: E402
from app.store import Store  # noqa: E402


def _day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "-"


def main(argv: list[str]) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "users":
        with store._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
        for r in rows:
            plan = effective_plan(r)
            until = f" until {_day(r['plan_expires_at'])}" if r.get("plan_expires_at") and plan != "free" else ""
            verified = "verified" if r.get("email_verified_at") else "unverified"
            print(f"{r['id']:>4}  {plan:<5}{until:<17} {'admin' if r['is_admin'] else '     '}  {verified:<10}  "
                  f"{_day(r['created_at'])}  {r['email']}")
        return 0
    if cmd == "requests":
        for r in store.upgrade_requests():
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['created_at']))}  wants {r['requested']:<6} "
                  f"(now {r['plan']:<4}) {r['email']}  {r['note'] or ''}")
        return 0
    if cmd == "support":
        for m in store.support_messages():
            print(f"#{m['id']} {time.strftime('%Y-%m-%d %H:%M', time.gmtime(m['created_at']))}  {m['email']}  "
                  f"[{'emailed' if m['emailed'] else 'stored only'}]\n   {m['subject']}\n   {m['message'][:300]}\n")
        return 0
    if cmd == "jobs":
        print(JobQueue(settings.db_path).stats())
        return 0
    if cmd == "email":
        _print_email(Mailer(settings, store).describe())
        return 0
    if cmd == "test-email":
        if len(argv) < 2 or "@" not in argv[1]:
            print("Usage: python scripts/admin.py test-email you@example.com")
            return 1
        mailer = Mailer(settings, store)
        _print_email(mailer.describe())
        if not mailer.configured:
            mailer.test(argv[1])
            print(f"\nEmail isn't set up, so the test was saved to {mailer.outbox}/ instead of being sent.")
            print("To send for real, add the email settings (README → \"Turn on real email\") and run this again.")
            return 1
        print(f"\nSending a test email to {argv[1]} ...")
        try:
            mailer.test(argv[1])
        except MailError as exc:
            print(f"\n✗ Not sent.\n  {exc}")
            return 1
        print(f"\n✓ Sent. Check {argv[1]} (and the spam folder). The status page now shows Email as Working.")
        return 0
    if cmd in ("grant", "revoke") and len(argv) >= 2:
        user = store.user_by_email(argv[1].strip().lower())
        if not user:
            print(f"No user with email {argv[1]}")
            return 1
        if cmd == "revoke":
            store.set_plan(user["id"], "free")
            print(f"{user['email']} is now on Free.")
            return 0
        days = None
        if "--days" in argv:
            try:
                days = float(argv[argv.index("--days") + 1])
            except (IndexError, ValueError):
                print("--days needs a number, e.g. --days 10")
                return 1
        store.set_plan(user["id"], "pro", time.time() + days * 86_400 if days else None)
        print(f"{user['email']} is now on Pro" + (f" for {days:g} days (until {_day(time.time() + days * 86_400)})." if days else "."))
        return 0
    print(__doc__)
    return 1


def _print_email(info: dict) -> None:
    print(f"Email:       {'set up' if info['configured'] else 'NOT set up (saved to outbox/)'}")
    print(f"Sends via:   {info['via']}")
    if info["configured"]:
        print(f"From:        {info['from']}")
    print(f"Email links: {info['public_url']}")
    last = info.get("last")
    if last:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last["at"]))
        print(f"Last email:  {'sent' if last['ok'] else 'FAILED'} at {when}" + ("" if last["ok"] else f"\n             {last['error']}"))
    for w in info.get("warnings", []):
        print(f"! {w}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
