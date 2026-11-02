"""Create restricted local snapshots of private configuration files."""

from __future__ import annotations

import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BACKUP_PATTERN = "private-*.tar.gz"
KEEP_BACKUPS = 20


def create_backup(private_dir: Path) -> Path:
    """Archive private configuration without following symbolic links.

    Args:
        private_dir: Directory containing local private configuration.

    Returns:
        Path to the completed archive.

    Raises:
        OSError: A file cannot be read or the archive cannot be written.
        ValueError: The directory is missing, symbolic, or contains links.
    """
    if private_dir.is_symlink() or not private_dir.is_dir():
        raise ValueError("private configuration directory is missing or is a symlink")
    backup_dir = private_dir / "backups"
    if backup_dir.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    configuration_paths = []
    for configuration_path in private_dir.rglob("*"):
        relative_path = configuration_path.relative_to(private_dir)
        if (
            relative_path.parts[0] == "backups"
            or relative_path.name == ".copy-reminder"
        ):
            continue
        if configuration_path.is_symlink():
            raise ValueError("configuration symlinks are unsupported")
        if configuration_path.is_file():
            configuration_paths.append(configuration_path)
    if not configuration_paths:
        raise ValueError("no private configuration files found")
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_path = backup_dir / f"private-{timestamp}.tar.gz"
    partial_path = archive_path.with_suffix(".partial")
    try:
        with partial_path.open("xb") as archive_file:
            os.chmod(partial_path, 0o600)
            with tarfile.open(fileobj=archive_file, mode="w:gz") as archive:
                for configuration_path in sorted(configuration_paths):
                    archive.add(
                        configuration_path,
                        arcname=str(configuration_path.relative_to(private_dir)),
                        recursive=False,
                    )
        partial_path.replace(archive_path)
    finally:
        partial_path.unlink(missing_ok=True)
    expired_backups = sorted(backup_dir.glob(BACKUP_PATTERN), reverse=True)[
        KEEP_BACKUPS:
    ]
    for expired_backup in expired_backups:
        expired_backup.unlink()
    return archive_path
