"""Store private credentials and resolve their local status safely."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key

from zata_ops.private_config_backup import create_backup

SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def credential_status(secret_path: Path) -> dict[str, bool]:
    """Return configured status by reference without exposing secret values."""
    if secret_path.is_symlink():
        raise ValueError("symlink credential file")
    if not secret_path.exists():
        return {}
    stored_credentials = dotenv_values(secret_path, interpolate=False, encoding="utf-8")
    return {
        credential_name: bool(credential_value)
        for credential_name, credential_value in stored_credentials.items()
        if SECRET_NAME.fullmatch(credential_name)
    }


def validate_private_directory(private_dir: Path) -> None:
    """Require an ignored, untracked private directory within this checkout."""
    repository_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
        check=False,
        text=True,
    )
    if repository_result.returncode != 0:
        raise ValueError("current directory is not inside a Git checkout")
    repository_root = Path(repository_result.stdout.strip()).resolve()
    absolute_directory = private_dir.absolute()
    if any(
        parent.is_symlink()
        for parent in (absolute_directory, *absolute_directory.parents)
    ):
        raise ValueError("symlink path")
    resolved_directory = absolute_directory.resolve()
    relative_directory = resolved_directory.relative_to(repository_root)
    ignored_result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--quiet",
            "--",
            str(relative_directory / "secrets.env"),
        ],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    tracked_result = subprocess.run(
        ["git", "ls-files", "--", str(relative_directory)],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    if (
        ignored_result.returncode != 0
        or tracked_result.returncode != 0
        or tracked_result.stdout
    ):
        raise ValueError("private directory must be ignored and untracked")
    resolved_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_directory.chmod(0o700)
    secret_path = resolved_directory / "secrets.env"
    if secret_path.is_symlink():
        raise ValueError("symlink credential file")
    if secret_path.exists():
        secret_path.chmod(0o600)


def store_secret(secret_path: Path, name: str, value: str) -> None:
    """Back up existing private configuration and atomically store a secret.

    Args:
        secret_path: Local dotenv file.
        name: Valid environment variable name.
        value: Single-line credential value.

    Raises:
        OSError: Backup or atomic write fails.
        ValueError: Reference or value is invalid.
    """
    if not SECRET_NAME.fullmatch(name):
        raise ValueError("invalid credential name")
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("invalid credential value")
    private_dir = secret_path.parent
    existing_files = [
        configuration_path
        for configuration_path in private_dir.iterdir()
        if configuration_path.name not in {"backups", ".copy-reminder"}
    ]
    if existing_files:
        create_backup(private_dir)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secret-", dir=private_dir
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(temporary_descriptor, "w", encoding="utf-8") as secret_file:
            if secret_path.exists():
                secret_file.write(secret_path.read_text(encoding="utf-8"))
        set_key(temporary_path, name, value, quote_mode="always", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(secret_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def remove_secret(secret_path: Path, name: str) -> None:
    """Back up private configuration and atomically remove one credential.

    Args:
        secret_path: Local dotenv file.
        name: Valid environment variable name.

    Raises:
        OSError: Backup or atomic write fails.
        ValueError: Reference is invalid or the credential file is a symlink.
    """
    if not SECRET_NAME.fullmatch(name):
        raise ValueError("invalid credential name")
    if secret_path.is_symlink():
        raise ValueError("symlink credential file")
    if not secret_path.exists():
        return
    source_text = secret_path.read_text(encoding="utf-8")
    assignment_pattern = re.compile(
        rf"(?m)^[ \t]*(?:export[ \t]+)?{re.escape(name)}[ \t]*=.*(?:\r?\n|$)"
    )
    updated_text, removed_count = assignment_pattern.subn("", source_text)
    if not removed_count:
        return
    private_dir = secret_path.parent
    existing_files = [
        configuration_path
        for configuration_path in private_dir.iterdir()
        if configuration_path.name not in {"backups", ".copy-reminder"}
    ]
    if existing_files:
        create_backup(private_dir)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secret-", dir=private_dir
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(temporary_descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(updated_text)
        temporary_path.chmod(0o600)
        temporary_path.replace(secret_path)
    finally:
        temporary_path.unlink(missing_ok=True)
