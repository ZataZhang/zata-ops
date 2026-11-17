"""Exercise real age encryption, authenticated restore, and failure isolation."""

import pytest

from zata_ops.private_config_archive import restore_backup
from zata_ops.private_config_backup import create_backup


def test_encrypted_backup_restore_and_wrong_identity(tmp_path, monkeypatch):
    private_directory = tmp_path / "private"
    private_directory.mkdir()
    (private_directory / "secrets.env").write_text(
        "TOKEN=synthetic-secret", encoding="utf-8"
    )
    archive_path = create_backup(private_directory)
    assert archive_path.name.endswith(".tar.gz.age")
    assert archive_path.read_bytes().startswith(b"age-encryption.org/v1\n")
    assert b"synthetic-secret" not in archive_path.read_bytes()
    assert archive_path.stat().st_mode & 0o777 == 0o600
    restore_directory = tmp_path / "restored"
    preview = restore_backup(archive_path, restore_directory)
    assert preview.member_paths == ("secrets.env",)
    assert not restore_directory.exists()
    restore_backup(archive_path, restore_directory, apply_restore=True)
    assert (restore_directory / "secrets.env").read_text(
        encoding="utf-8"
    ) == "TOKEN=synthetic-secret"
    monkeypatch.setenv("ZATA_BACKUP_IDENTITY", str(tmp_path / "missing-key"))
    with pytest.raises(ValueError, match="decryption failed"):
        restore_backup(archive_path, restore_directory, apply_restore=True)
    assert (restore_directory / "secrets.env").exists()


def test_encryption_failure_keeps_existing_backups(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("synthetic = true", encoding="utf-8")
    archive_path = create_backup(tmp_path)
    monkeypatch.setenv("ZATA_BACKUP_RECIPIENT", str(tmp_path / "missing.pub"))
    with pytest.raises(ValueError, match="encryption failed"):
        create_backup(tmp_path)
    assert list((tmp_path / "backups").iterdir()) == [archive_path]


def test_tampered_archive_is_rejected(tmp_path):
    (tmp_path / "config.toml").write_text("synthetic = true", encoding="utf-8")
    archive_path = create_backup(tmp_path)
    encrypted_payload = bytearray(archive_path.read_bytes())
    encrypted_payload[-1] ^= 1
    archive_path.write_bytes(encrypted_payload)
    with pytest.raises(ValueError, match="decryption failed"):
        restore_backup(archive_path, tmp_path / "restored", apply_restore=True)
    assert not (tmp_path / "restored").exists()


def test_legacy_restore_and_encrypted_retention(tmp_path, monkeypatch):
    import tarfile

    from zata_ops import private_config_backup

    private_directory = tmp_path / "private"
    private_directory.mkdir()
    configuration_path = private_directory / "config.toml"
    configuration_path.write_text("synthetic = true", encoding="utf-8")
    legacy_path = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy_path, "w:gz") as archive:
        archive.add(configuration_path, arcname="config.toml")
    restore_backup(legacy_path, tmp_path / "restored", apply_restore=True)
    monkeypatch.setattr(private_config_backup, "KEEP_BACKUPS", 2)
    archive_paths = [create_backup(private_directory) for _ in range(3)]
    assert not archive_paths[0].exists()
    assert all(archive_path.exists() for archive_path in archive_paths[1:])


def test_persistent_recipient_list_survives_new_process(
    tmp_path, monkeypatch, backup_test_identity
):
    import os
    import subprocess
    from pathlib import Path

    home_directory = tmp_path / "home"
    ssh_directory = home_directory / ".ssh"
    ssh_directory.mkdir(parents=True)
    recovery_identity = ssh_directory / "recovery.key"
    subprocess.run(
        ["age-keygen", "-o", str(recovery_identity)],
        check=True,
        capture_output=True,
    )
    recovery_public_key = subprocess.run(
        ["age-keygen", "-y", str(recovery_identity)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (ssh_directory / "zata-backup-recipients.txt").write_text(
        Path(str(backup_test_identity) + ".pub").read_text(encoding="utf-8")
        + recovery_public_key,
        encoding="utf-8",
    )
    private_directory = tmp_path / "private"
    private_directory.mkdir()
    (private_directory / "test.txt").write_text("synthetic", encoding="utf-8")
    subprocess_environment = dict(os.environ, HOME=str(home_directory))
    subprocess_environment.pop("ZATA_BACKUP_RECIPIENT", None)
    subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/diagnostics/private_config_backup.py",
            "backup",
            "--private-dir",
            str(private_directory),
        ],
        env=subprocess_environment,
        check=True,
        capture_output=True,
    )
    archive_path = next((private_directory / "backups").glob("*.age"))
    for identity_path in (backup_test_identity, recovery_identity):
        monkeypatch.setenv("ZATA_BACKUP_IDENTITY", str(identity_path))
        assert restore_backup(archive_path, private_directory).member_paths == (
            "test.txt",
        )
