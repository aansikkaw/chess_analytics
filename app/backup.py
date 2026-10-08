"""Database backups: snapshot, compress, (optionally) encrypt, upload, prune, restore, verify.

The database is the only thing to back up: knowledge bundles are rebuilt from it on demand.

Where backups go (BACKUP_TARGET):
  s3://bucket/prefix   any S3-compatible storage: AWS S3, Cloudflare R2, Backblaze B2, Hetzner,
                       DigitalOcean Spaces... Set S3_ENDPOINT_URL for non-AWS providers, plus
                       AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (and S3_REGION if needed).
  /some/folder         a local or mounted folder (fine for testing; not a real off-site backup).

Optional: BACKUP_ENCRYPTION_KEY (any long passphrase) encrypts each file before upload, so the
storage provider never sees emails or password hashes. Keep the passphrase somewhere safe:
without it, backups can't be restored.

BACKUP_KEEP (default 14) daily backups are kept; older ones are deleted.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PREFIX = "plateau-"
SUFFIX = ".sqlite.gz"
ENC_SUFFIX = ".enc"
REQUIRED_TABLES = {"users", "accounts", "games", "puzzles", "sessions"}


class BackupError(RuntimeError):
    pass


@dataclass
class BackupConfig:
    target: str
    keep: int = 14
    endpoint_url: str | None = None
    region: str | None = None
    passphrase: str | None = None

    @classmethod
    def from_env(cls) -> BackupConfig:
        target = os.getenv("BACKUP_TARGET", "").strip()
        if not target:
            raise BackupError("Set BACKUP_TARGET (s3://bucket/prefix or a folder path) to enable backups.")
        return cls(target=target, keep=int(os.getenv("BACKUP_KEEP", "14")), endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
                   region=os.getenv("S3_REGION") or None, passphrase=os.getenv("BACKUP_ENCRYPTION_KEY") or None)


# ---- storage backends -------------------------------------------------------------------------
class FolderStorage:
    def __init__(self, root: str):
        self.root = Path(root.removeprefix("file://")).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, local: Path, name: str) -> None:
        tmp = self.root / (name + ".part")
        shutil.copyfile(local, tmp)
        tmp.replace(self.root / name)

    def get(self, name: str, local: Path) -> None:
        shutil.copyfile(self.root / name, local)

    def list(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.name.startswith(PREFIX) and not p.name.endswith(".part"))

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, url: str, endpoint_url: str | None, region: str | None):
        try:
            import boto3
        except ImportError as exc:
            raise BackupError("S3 backups need boto3: pip install boto3") from exc
        rest = url.removeprefix("s3://")
        self.bucket, _, prefix = rest.partition("/")
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def put(self, local: Path, name: str) -> None:
        self.s3.upload_file(str(local), self.bucket, self.prefix + name)

    def get(self, name: str, local: Path) -> None:
        self.s3.download_file(self.bucket, self.prefix + name, str(local))

    def list(self) -> list[str]:
        names, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.prefix + PREFIX}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            names += [o["Key"][len(self.prefix):] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(names)
            token = r["NextContinuationToken"]

    def delete(self, name: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self.prefix + name)


def storage_for(cfg: BackupConfig):
    return S3Storage(cfg.target, cfg.endpoint_url, cfg.region) if cfg.target.startswith("s3://") else FolderStorage(cfg.target)


# ---- encryption (optional) -----------------------------------------------------------------
def _fernet(passphrase: str, salt: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise BackupError("BACKUP_ENCRYPTION_KEY needs the 'cryptography' package: pip install cryptography") from exc
    key = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(path: Path, passphrase: str) -> Path:
    salt = os.urandom(16)
    out = path.with_name(path.name + ENC_SUFFIX)
    out.write_bytes(b"PBK1" + salt + _fernet(passphrase, salt).encrypt(path.read_bytes()))
    return out


def _decrypt(path: Path, passphrase: str | None) -> Path:
    data = path.read_bytes()
    if not data.startswith(b"PBK1"):
        raise BackupError("Not an encrypted Plateau Breaker backup.")
    if not passphrase:
        raise BackupError("This backup is encrypted: set BACKUP_ENCRYPTION_KEY to the passphrase used when it was made.")
    from cryptography.fernet import InvalidToken

    try:
        plain = _fernet(passphrase, data[4:20]).decrypt(data[20:])
    except InvalidToken as exc:
        raise BackupError("Wrong BACKUP_ENCRYPTION_KEY for this backup.") from exc
    out = path.with_name(path.name.removesuffix(ENC_SUFFIX))
    out.write_bytes(plain)
    return out


# ---- the operations -----------------------------------------------------------------------------
def snapshot(db_path: str, dest: Path) -> None:
    """A consistent copy of a live SQLite database (safe while the app is writing)."""
    src = sqlite3.connect(db_path, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def verify_db(path: Path) -> dict:
    """Integrity check plus a row count per table. Raises BackupError if the file isn't a healthy app database."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"Can't open the backup: {exc}") from exc
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise BackupError(f"Integrity check failed: {ok}")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise BackupError(f"Backup is missing tables: {', '.join(sorted(missing))}")
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(REQUIRED_TABLES)}  # noqa: S608
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Not a valid database: {exc}") from exc
    finally:
        con.close()
    return counts


def run_backup(db_path: str, cfg: BackupConfig) -> dict:
    """Snapshot → verify → gzip → (encrypt) → upload → prune. Returns what was stored."""
    storage = storage_for(cfg)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}"  # sorts by time
    with tempfile.TemporaryDirectory(prefix="pb-backup-") as tmp:
        raw = Path(tmp) / "snapshot.sqlite"
        snapshot(db_path, raw)
        counts = verify_db(raw)
        gz = Path(tmp) / f"{PREFIX}{stamp}{SUFFIX}"
        with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
        upload = _encrypt(gz, cfg.passphrase) if cfg.passphrase else gz
        digest = hashlib.sha256(upload.read_bytes()).hexdigest()
        storage.put(upload, upload.name)
        size = upload.stat().st_size
    pruned = prune(storage, cfg.keep)
    return {"ok": True, "name": upload.name, "bytes": size, "sha256": digest, "counts": counts, "pruned": pruned,
            "encrypted": bool(cfg.passphrase), "target": _redact(cfg.target)}


def prune(storage, keep: int) -> list[str]:
    names = storage.list()
    old = names[:-keep] if keep > 0 and len(names) > keep else []
    for n in old:
        storage.delete(n)
    return old


def fetch(cfg: BackupConfig, name: str | None, dest: Path) -> dict:
    """Download a backup (the newest by default), decrypt, decompress to `dest`, verify. Never touches the live DB."""
    storage = storage_for(cfg)
    names = storage.list()
    if not names:
        raise BackupError("No backups found at the target.")
    name = name or names[-1]
    if name not in names:
        raise BackupError(f"No backup called {name}.")
    with tempfile.TemporaryDirectory(prefix="pb-restore-") as tmp:
        local = Path(tmp) / name
        storage.get(name, local)
        if name.endswith(ENC_SUFFIX):
            local = _decrypt(local, cfg.passphrase)
        tmp_db = Path(tmp) / "restored.sqlite"
        with gzip.open(local, "rb") as fi, open(tmp_db, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        counts = verify_db(tmp_db)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_db, dest)
    return {"ok": True, "name": name, "counts": counts, "restored_to": str(dest)}


def restore_test(db_path: str, cfg: BackupConfig) -> dict:
    """Restore the newest backup to a temp file and compare it with the live database."""
    with tempfile.TemporaryDirectory(prefix="pb-rtest-") as tmp:
        out = fetch(cfg, None, Path(tmp) / "check.sqlite")
    live = verify_db(Path(db_path))
    # Rows are deleted legitimately (sessions expire, people unlink accounts), so counts aren't compared strictly.
    # What would be wrong: a backup that's empty while the live database has users.
    empty = live.get("users", 0) > 0 and out["counts"].get("users", 0) == 0
    out.update({"live_counts": live, "ok": not empty,
                "detail": "The newest backup has no users although the live database does." if empty else
                "Newest backup restored and passed the integrity check."})
    return out


def _redact(target: str) -> str:
    return target if target.startswith("s3://") else str(Path(target).name)
