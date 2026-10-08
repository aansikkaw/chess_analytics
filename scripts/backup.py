"""Back up and restore the Plateau Breaker database.

  python scripts/backup.py run                    snapshot now, upload, prune old backups
  python scripts/backup.py list                   list stored backups
  python scripts/backup.py test-restore           restore the newest backup to a temp file and verify it
  python scripts/backup.py restore --to PATH [--name NAME]
                                                  download a backup to PATH (never overwrites the live DB)

Settings come from environment variables: DB_PATH, BACKUP_TARGET, BACKUP_KEEP, S3_ENDPOINT_URL,
S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, BACKUP_ENCRYPTION_KEY. See app/backup.py.
In production the scheduler (python -m app.scheduler) runs `run` daily and `test-restore` weekly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.backup import BackupConfig, BackupError, fetch, restore_test, run_backup, storage_for  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.store import Store  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "list", "restore", "test-restore"])
    ap.add_argument("--to", help="where to write a restored database (restore)")
    ap.add_argument("--name", help="which backup to restore (default: newest)")
    ap.add_argument("--force", action="store_true", help="allow --to to point at the live database (stop the app first!)")
    args = ap.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path) if args.cmd in ("run", "test-restore") else None  # restore must work on a corrupt DB
    try:
        cfg = BackupConfig.from_env()
        if args.cmd == "run":
            out = run_backup(settings.db_path, cfg)
            store.kv_set("last_backup", out)
        elif args.cmd == "list":
            out = {"backups": storage_for(cfg).list()}
        elif args.cmd == "test-restore":
            out = restore_test(settings.db_path, cfg)
            store.kv_set("last_restore_test", out)
        else:
            if not args.to:
                ap.error("restore needs --to PATH")
            dest = Path(args.to).resolve()
            if dest == Path(settings.db_path).resolve() and not args.force:
                ap.error("--to is the live database. Stop the app, then add --force (or restore elsewhere and swap files).")
            out = fetch(cfg, args.name, dest)
    except BackupError as exc:
        if store is not None:
            store.kv_set("last_backup" if args.cmd == "run" else "last_restore_test", {"ok": False, "error": str(exc)})
        print(f"Backup error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
