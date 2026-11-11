"""Local-directory backup store with the same interface as :class:`S3Client`.

Backups land under ``<root>/<prefix>/<YYYY-MM-DD_HHMMSS>/<full|incremental>/``,
mirroring the S3 key layout, so manifests keep their ``s3_key`` field and
``zata-ops db restore`` can read either store.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zata_ops.db._s3 import S3Client


class LocalBackupStore:
    """Store backup artifacts in a local directory instead of S3.

    Args:
        root: Directory that holds the backup tree; created on first write.
        prefix: Key prefix, the first path segment under ``root``.
    """

    def __init__(self, root: str | Path, prefix: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.prefix = prefix

    def _path_for(self, key: str) -> Path:
        """Map a backup key to a path that must stay inside :attr:`root`.

        Args:
            key: Backup key such as ``prefix/2026-11-11_020000/full/database.sql.gz``.

        Returns:
            Absolute path under :attr:`root`.

        Raises:
            ValueError: If the key escapes the backup root.
        """
        target_path = (self.root / key).resolve()
        if not target_path.is_relative_to(self.root):
            raise ValueError(f"backup key escapes the local backup directory: {key}")
        return target_path

    def upload_file(self, file_path: Path, key: str) -> None:
        """Copy a staged file into the backup tree.

        Args:
            file_path: Local file to store.
            key: Destination key relative to :attr:`root`.
        """
        target_path = self._path_for(key)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, target_path)

    def upload_json(self, data: dict, key: str) -> None:
        """Write a JSON document into the backup tree.

        Args:
            data: Dictionary to serialise.
            key: Destination key relative to :attr:`root`.
        """
        target_path = self._path_for(key)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def download_file(self, s3_key: str, output_path: Path) -> None:
        """Copy a stored file to ``output_path``.

        Args:
            s3_key: Stored key, as recorded in the manifest.
            output_path: Local destination path.
        """
        shutil.copyfile(self._path_for(s3_key), output_path)

    def list_backup_dates(self) -> list[str]:
        """Return sorted backup date directories under :attr:`prefix`.

        Returns:
            Sorted list of ``YYYY-MM-DD_HHMMSS`` directory names.
        """
        prefix_dir = self._path_for(self.prefix)
        if not prefix_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in prefix_dir.iterdir()
            if entry.is_dir() and S3Client._BACKUP_DIR_RE.match(entry.name)
        )

    def get_backup_manifest(self, date_str: str) -> dict | None:
        """Read ``manifest.json`` for a backup date (full first, then incremental).

        Args:
            date_str: Backup date string (``YYYY-MM-DD_HHMMSS``).

        Returns:
            Parsed manifest dictionary, or ``None`` if not found.
        """
        for backup_type in ("full", "incremental"):
            manifest_path = self._path_for(
                f"{self.prefix}/{date_str}/{backup_type}/manifest.json"
            )
            if manifest_path.is_file():
                return json.loads(manifest_path.read_text(encoding="utf-8"))
        return None

    def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
        """Delete backup directories whose timestamp is older than ``retention_days``.

        The age comes from the directory name, not file modification times, so
        copying or restoring the tree does not reset retention.

        Args:
            prefix: Backup key prefix to scan.
            retention_days: Retention window in days.

        Returns:
            Number of deleted files.
        """
        prefix_dir = self._path_for(prefix)
        if not prefix_dir.is_dir():
            return 0
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted_file_count = 0
        for backup_dir in prefix_dir.iterdir():
            if not backup_dir.is_dir() or not S3Client._BACKUP_DIR_RE.match(
                backup_dir.name
            ):
                continue
            backup_dt = datetime.strptime(backup_dir.name, "%Y-%m-%d_%H%M%S").replace(
                tzinfo=timezone.utc
            )
            if backup_dt >= cutoff_dt:
                continue
            deleted_file_count += sum(
                1 for path in backup_dir.rglob("*") if path.is_file()
            )
            shutil.rmtree(backup_dir)
        return deleted_file_count


class FanoutBackupStore:
    """Write every artifact to several stores; reads are not supported.

    Args:
        stores: Backup stores sharing the :class:`S3Client` write interface.
    """

    def __init__(self, stores: list) -> None:
        self.stores = stores

    def upload_file(self, file_path: Path, key: str) -> None:
        """Store a file in every configured store."""
        for store in self.stores:
            store.upload_file(file_path, key)

    def upload_json(self, data: dict, key: str) -> None:
        """Store a JSON document in every configured store."""
        for store in self.stores:
            store.upload_json(data, key)

    def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
        """Apply retention in every store and return the total deletions."""
        return sum(
            store.cleanup_old_backups(prefix, retention_days) for store in self.stores
        )
