"""Create restricted local snapshots of private configuration files."""

from __future__ import annotations

import io
import os
import subprocess
from contextlib import contextmanager
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

BACKUP_PATTERN = "private-*.tar.gz.age"
MAX_ENCRYPTED_PAYLOAD_BYTES = 512 * 1024 * 1024
KEEP_BACKUPS = 20


@contextmanager
def open_backup_archive(archive_path: Path) -> Iterator[tarfile.TarFile]:
    """Open an encrypted backup or a legacy gzip archive.

    Args:
        archive_path: Archive to authenticate, decrypt, and inspect.

    Yields:
        Readable tar archive with the existing restore validation applied later.

    Raises:
        OSError: The archive or age executable cannot be accessed.
        ValueError: Decryption fails or the compressed payload exceeds the limit.
        tarfile.TarError: The decrypted payload is not a gzip tar archive.
    """
    if archive_path.suffix != ".age":
        with tarfile.open(archive_path, mode="r:gz") as archive:
            yield archive
        return
    identity_path = Path(
        os.environ.get("ZATA_BACKUP_IDENTITY", "~/.ssh/id_ed25519")
    ).expanduser()
    with subprocess.Popen(
        ["age", "--decrypt", "-i", str(identity_path), str(archive_path.resolve())],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as decrypt_process:
        assert decrypt_process.stdout is not None
        try:
            compressed_payload = decrypt_process.stdout.read(
                MAX_ENCRYPTED_PAYLOAD_BYTES + 1
            )
            if len(compressed_payload) > MAX_ENCRYPTED_PAYLOAD_BYTES:
                raise ValueError("decrypted compressed archive exceeds size limit")
            if decrypt_process.wait() != 0:
                raise ValueError(
                    "age decryption failed; check backup identity and archive"
                )
        finally:
            if decrypt_process.poll() is None:
                decrypt_process.kill()
                decrypt_process.wait()
    with tarfile.open(fileobj=io.BytesIO(compressed_payload), mode="r:gz") as archive:
        yield archive


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
    """Encrypt private configuration to an SSH public key without following symlinks.

    Args:
        private_dir: Directory containing local private configuration. Encryption uses
            ZATA_BACKUP_RECIPIENT, the persistent backup recipients file when
            present, or the default SSH Ed25519 public key file.
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
    archive_path = backup_dir / f"private-{timestamp}.tar.gz.age"
    partial_path = archive_path.with_suffix(".partial")
    try:
        persistent_recipients_path = Path.home() / ".ssh/zata-backup-recipients.txt"
        default_recipient_path = (
            persistent_recipients_path
            if persistent_recipients_path.exists()
            else Path.home() / ".ssh/id_ed25519.pub"
        )
        recipient_path = Path(
            os.environ.get("ZATA_BACKUP_RECIPIENT", str(default_recipient_path))
        ).expanduser()
        with io.BytesIO() as archive_buffer:
            with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
                for configuration_path in sorted(configuration_paths):
                    archive.add(
                        configuration_path,
                        arcname=str(configuration_path.relative_to(private_dir)),
                        recursive=False,
                    )
            with partial_path.open("xb") as encrypted_file:
                os.chmod(partial_path, 0o600)
                encryption_result = subprocess.run(
                    ["age", "--encrypt", "-R", str(recipient_path)],
                    input=archive_buffer.getvalue(),
                    stdout=encrypted_file,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if encryption_result.returncode != 0:
                    raise ValueError(
                        "age encryption failed; no plaintext backup was saved"
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
