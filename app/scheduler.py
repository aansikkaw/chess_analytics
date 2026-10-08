"""Timed chores:  python -m app.scheduler   (or --once from cron)

  * daily database backup            (needs BACKUP_TARGET)          at BACKUP_HOUR_UTC (default 21, i.e. 02:30 IST)
  * weekly restore test of a backup  (needs BACKUP_TARGET)          Sundays, an hour after the backup
  * weekly email digest              (needs SMTP or DIGEST_ENABLED) Mondays at DIGEST_HOUR_UTC (default 3, i.e. 08:30 IST)

Last-run times are kept in the database, so restarting the scheduler never runs a chore twice,
and the status page can show when the last backup and restore test happened.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .backup import BackupConfig, BackupError, restore_test, run_backup
from .config import load_settings
from .digest import send_digests
from .mailer import Mailer
from .observability import init_sentry
from .store import Store

log = logging.getLogger("plateau.scheduler")
DAY = 86_400


@dataclass
class Chore:
    name: str
    period_s: float
    hour_utc: int
    weekday: int | None  # 0 = Monday
    enabled: Callable[[], bool]
    run: Callable[[], dict]


def due(chore: Chore, now: float, last_run: float | None) -> bool:
    t = time.gmtime(now)
    if chore.weekday is not None and t.tm_wday != chore.weekday:
        return False
    if t.tm_hour < chore.hour_utc:
        return False
    return last_run is None or now - last_run > chore.period_s - 3 * 3600


def chores(settings, store: Store, mailer: Mailer) -> list[Chore]:
    backup_hour = int(os.getenv("BACKUP_HOUR_UTC", "21"))

    def do_backup() -> dict:
        out = run_backup(settings.db_path, BackupConfig.from_env())
        store.kv_set("last_backup", out)
        return out

    def do_restore_test() -> dict:
        out = restore_test(settings.db_path, BackupConfig.from_env())
        store.kv_set("last_restore_test", out)
        return out

    def do_digest() -> dict:
        return {"ok": True, "sent": send_digests(store, mailer, settings.public_url)}

    has_backup = lambda: bool(os.getenv("BACKUP_TARGET"))  # noqa: E731
    return [
        Chore("backup", DAY, backup_hour, None, has_backup, do_backup),
        Chore("restore_test", 7 * DAY, min(23, backup_hour + 1), 6, has_backup, do_restore_test),
        Chore("digest", 7 * DAY, int(os.getenv("DIGEST_HOUR_UTC", "3")), 0,
              lambda: mailer.configured or os.getenv("DIGEST_ENABLED") == "1", do_digest),
    ]


def tick(store: Store, items: list[Chore], now: float | None = None) -> list[str]:
    now = now or time.time()
    ran = []
    for c in items:
        if not c.enabled():
            continue
        last = store.kv_get(f"sched:{c.name}")
        if not due(c, now, last["updated_at"] if last else None):
            continue
        try:
            out = c.run()
            store.kv_set(f"sched:{c.name}", {"ok": out.get("ok", True)})
            log.info("%s done: %s", c.name, {k: v for k, v in out.items() if k in ("ok", "name", "bytes", "sent", "detail")})
        except Exception as exc:  # noqa: BLE001 - S3/network/SMTP errors must not crash-loop the scheduler
            msg = str(exc) if isinstance(exc, (BackupError, OSError)) else f"{type(exc).__name__}: {exc}"
            store.kv_set(f"sched:{c.name}", {"ok": False, "error": msg[:500]})
            if c.name == "backup":
                store.kv_set("last_backup", {"ok": False, "error": msg[:500]})
            if c.name == "restore_test":
                store.kv_set("last_restore_test", {"ok": False, "error": msg[:500]})
            log.error("%s failed: %s", c.name, msg)
            _report(exc)
        ran.append(c.name)
    return ran


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run whatever is due now and exit (for cron)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    init_sentry(settings, "scheduler")
    store = Store(settings.db_path)
    items = chores(settings, store, Mailer(settings, store))
    while True:
        tick(store, items)
        if args.once:
            return
        time.sleep(600)


def _report(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass


if __name__ == "__main__":
    main()
