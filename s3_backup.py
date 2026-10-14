"""Consistent SQLite snapshots to S3-compatible object storage.

The live SQLite database always remains on a local filesystem.  This module
uses SQLite's online backup API, validates the snapshot, and only then uploads
it.  It can restore the latest validated snapshot before Finbot starts.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError


logging.basicConfig(
    level=os.getenv("FINBOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("finbot.s3_backup")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool
    database_path: Path
    bucket: str
    prefix: str
    region: str | None
    endpoint_url: str | None
    interval_seconds: int
    restore_mode: str
    restore_required: bool
    server_side_encryption: str
    kms_key_id: str | None

    @classmethod
    def from_environment(cls) -> "BackupSettings":
        restore_mode = os.getenv("FINBOT_S3_RESTORE_MODE", "if_newer").strip().lower()
        if restore_mode not in {"never", "if_missing", "if_newer", "always"}:
            raise ValueError("FINBOT_S3_RESTORE_MODE must be never, if_missing, if_newer, or always.")
        interval = int(os.getenv("FINBOT_S3_BACKUP_INTERVAL_SECONDS", "900"))
        if interval < 60:
            raise ValueError("FINBOT_S3_BACKUP_INTERVAL_SECONDS must be at least 60.")
        settings = cls(
            enabled=env_bool("FINBOT_S3_BACKUP_ENABLED"),
            database_path=Path(os.getenv("FINBOT_DB_PATH", "/data/finbot.db")).expanduser().resolve(),
            bucket=os.getenv("FINBOT_S3_BUCKET", "").strip(),
            prefix=os.getenv("FINBOT_S3_PREFIX", "finbot").strip().strip("/"),
            region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
            endpoint_url=os.getenv("FINBOT_S3_ENDPOINT_URL") or None,
            interval_seconds=interval,
            restore_mode=restore_mode,
            restore_required=env_bool("FINBOT_S3_RESTORE_REQUIRED"),
            server_side_encryption=os.getenv("FINBOT_S3_SERVER_SIDE_ENCRYPTION", "AES256").strip(),
            kms_key_id=os.getenv("FINBOT_S3_KMS_KEY_ID") or None,
        )
        if settings.enabled and not settings.bucket:
            raise ValueError("FINBOT_S3_BUCKET is required when FINBOT_S3_BACKUP_ENABLED=true.")
        return settings

    @property
    def latest_key(self) -> str:
        return f"{self.prefix + '/' if self.prefix else ''}latest/finbot.db"

    def versioned_key(self, timestamp: datetime) -> str:
        stamp = timestamp.astimezone(timezone.utc).strftime("%Y/%m/%d/finbot-%Y%m%dT%H%M%SZ.db")
        return f"{self.prefix + '/' if self.prefix else ''}snapshots/{stamp}"


class S3SQLiteBackup:
    def __init__(self, settings: BackupSettings):
        self.settings = settings
        client_options: dict[str, Any] = {}
        if settings.region:
            client_options["region_name"] = settings.region
        if settings.endpoint_url:
            client_options["endpoint_url"] = settings.endpoint_url
        self.s3 = boto3.client("s3", **client_options)

    def _upload_args(self, checksum: str, created_at: datetime) -> dict[str, Any]:
        args: dict[str, Any] = {
            "ContentType": "application/vnd.sqlite3",
            "Metadata": {
                "sha256": checksum,
                "created-at": created_at.astimezone(timezone.utc).isoformat(),
            },
        }
        if self.settings.server_side_encryption:
            args["ServerSideEncryption"] = self.settings.server_side_encryption
        if self.settings.server_side_encryption == "aws:kms" and self.settings.kms_key_id:
            args["SSEKMSKeyId"] = self.settings.kms_key_id
        return args

    @staticmethod
    def _validate_database(path: Path) -> None:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite snapshot validation failed: {result!r}")
        finally:
            connection.close()

    def create_snapshot(self) -> dict[str, Any]:
        source_path = self.settings.database_path
        if not source_path.is_file():
            raise FileNotFoundError(f"SQLite database does not exist yet: {source_path}")
        created_at = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory(prefix="finbot-s3-backup-") as temporary_directory:
            snapshot_path = Path(temporary_directory) / "finbot.db"
            source = sqlite3.connect(source_path, timeout=30)
            destination = sqlite3.connect(snapshot_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            self._validate_database(snapshot_path)
            checksum = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            upload_args = self._upload_args(checksum, created_at)
            versioned_key = self.settings.versioned_key(created_at)
            self.s3.upload_file(
                str(snapshot_path), self.settings.bucket, versioned_key, ExtraArgs=upload_args
            )
            self.s3.upload_file(
                str(snapshot_path), self.settings.bucket, self.settings.latest_key, ExtraArgs=upload_args
            )
        logger.info(
            "Uploaded validated SQLite snapshot to s3://%s/%s",
            self.settings.bucket,
            versioned_key,
        )
        return {"bucket": self.settings.bucket, "key": versioned_key, "sha256": checksum}

    def restore_latest(self) -> bool:
        mode = self.settings.restore_mode
        database_path = self.settings.database_path
        if mode == "never" or (mode == "if_missing" and database_path.exists()):
            logger.info("S3 restore skipped by mode %s.", mode)
            return False
        try:
            remote = self.s3.head_object(Bucket=self.settings.bucket, Key=self.settings.latest_key)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"} and not self.settings.restore_required:
                logger.warning("No S3 database snapshot exists yet; Finbot will initialize a new database.")
                return False
            raise
        remote_modified = remote.get("LastModified")
        if mode == "if_newer" and database_path.exists() and remote_modified:
            local_modified = datetime.fromtimestamp(database_path.stat().st_mtime, timezone.utc)
            if local_modified >= remote_modified.astimezone(timezone.utc):
                logger.info("Local SQLite database is newer than or equal to the S3 snapshot; restore skipped.")
                return False
        database_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = database_path.with_name(f".{database_path.name}.restore-{uuid4().hex}.tmp")
        try:
            self.s3.download_file(self.settings.bucket, self.settings.latest_key, str(temporary_path))
            self._validate_database(temporary_path)
            expected_checksum = (remote.get("Metadata") or {}).get("sha256")
            actual_checksum = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
            if expected_checksum and actual_checksum != expected_checksum:
                raise RuntimeError("Downloaded SQLite snapshot checksum does not match S3 metadata.")
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(f"{database_path}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(temporary_path, database_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        logger.info(
            "Restored validated SQLite snapshot from s3://%s/%s",
            self.settings.bucket,
            self.settings.latest_key,
        )
        return True


def run_daemon(backup: S3SQLiteBackup) -> None:
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("S3 backup daemon started; interval=%s seconds", backup.settings.interval_seconds)
    # Give the scheduler a moment to initialize a brand-new database, then
    # produce the first recovery point without waiting for the full interval.
    delay = min(10, backup.settings.interval_seconds)
    while not stopping.wait(delay):
        try:
            backup.create_snapshot()
        except Exception:
            logger.exception("Scheduled S3 database backup failed; the daemon will retry.")
        delay = backup.settings.interval_seconds
    try:
        backup.create_snapshot()
    except FileNotFoundError:
        logger.warning("No database exists for the final S3 backup.")
    except Exception:
        logger.exception("Final S3 database backup failed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore Finbot SQLite through S3.")
    parser.add_argument("command", choices=("restore", "backup", "daemon"))
    command = parser.parse_args().command
    settings = BackupSettings.from_environment()
    if not settings.enabled:
        logger.info("S3 database backup is disabled.")
        return
    backup = S3SQLiteBackup(settings)
    if command == "restore":
        backup.restore_latest()
    elif command == "backup":
        backup.create_snapshot()
    else:
        run_daemon(backup)


if __name__ == "__main__":
    main()
