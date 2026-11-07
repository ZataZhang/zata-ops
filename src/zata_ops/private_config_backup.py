"""Create restricted local snapshots of private configuration files."""

from __future__ import annotations

import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BACKUP_PATTERN = "private-*.tar.gz"
KEEP_BACKUPS = 20


def _list_configuration_files(private_dir: Path) -> list[Path]:
    if private_dir.is_symlink() or not private_dir.is_dir():
        raise ValueError("private configuration directory is missing or is a symlink")
    backup_dir = private_dir / "backups"
    if backup_dir.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    reminder_path = private_dir / ".copy-reminder"
    if reminder_path.is_symlink():
        raise ValueError("reminder state must not be a symlink")
    if reminder_path.exists() and not reminder_path.is_file():
        raise ValueError("reminder state must be a regular file")

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
        elif not configuration_path.is_dir():
            raise ValueError("special configuration files are unsupported")
    return configuration_paths


def create_backup(
    private_dir: Path,
    *,
    protected_backup_paths: tuple[Path, ...] = (),
) -> Path:
    """Archive private configuration without following symbolic links.

    Args:
        private_dir: Directory containing local private configuration.
        protected_backup_paths: Archives that retention must not remove.

    Returns:
        Path to the completed archive.

    Raises:
        OSError: A file cannot be read or the archive cannot be written.
        ValueError: The directory is missing, symbolic, or contains unsupported files.
    """
    configuration_paths = _list_configuration_files(private_dir)
    if not configuration_paths:
        raise ValueError("no private configuration files found")
    backup_dir = private_dir / "backups"
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
    protected_backup_path_strings = {
        str(protected_backup_path.resolve())
        for protected_backup_path in protected_backup_paths
    }
    retained_backup_paths = [
        candidate_backup_path
        for candidate_backup_path in sorted(
            backup_dir.glob(BACKUP_PATTERN), reverse=True
        )
        if str(candidate_backup_path.resolve()) not in protected_backup_path_strings
    ]
    expired_backups = retained_backup_paths[KEEP_BACKUPS:]
    for expired_backup in expired_backups:
        expired_backup.unlink()
    return archive_path
