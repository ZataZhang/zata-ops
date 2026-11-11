"""Tests for local-directory backup storage in ``zata-ops db``."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.db import _backup_impl
from zata_ops.db._local_store import LocalBackupStore

BACKUP_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _options(
    tmp_path: Path, *, local_dir: str, s3_endpoint: str = ""
) -> _backup_impl.BackupRunOptions:
    """Build backup options that never touch real logs or resources."""
    return _backup_impl.BackupRunOptions(
        project="demo",
        database_url="postgresql://u:p@h:5432/db",
        s3_endpoint=s3_endpoint,
        s3_access_key="k" if s3_endpoint else "",
        s3_secret_key="s" if s3_endpoint else "",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        logs_dir=str(tmp_path / "missing-logs"),
        resources_dir=str(tmp_path / "missing-resources"),
        work_dir=str(tmp_path / "work"),
        retention_days=36500,  # 固定的 BACKUP_NOW 早于真实当前时间，避免刚写入就被保留策略清掉
        full_backup_day=6,
        force_full=True,
        local_dir=local_dir,
    )


@pytest.fixture(autouse=True)
def _fake_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace pg_dump with a fixed gzip payload."""
    monkeypatch.setattr(
        _backup_impl,
        "backup_database",
        lambda db_url, output_path: output_path.write_bytes(b"\x1f\x8bfake-dump"),
    )


def test_local_only_backup_writes_dump_and_manifest_without_s3(tmp_path: Path) -> None:
    local_dir = tmp_path / "backups"

    def forbidden_factory(**_: Any) -> None:
        raise AssertionError("S3 must not be used without an endpoint")

    backup_run_result = _backup_impl.run_backup(
        _options(tmp_path, local_dir=str(local_dir)),
        s3_client_factory=forbidden_factory,
        now=BACKUP_NOW,
    )

    backup_dir = local_dir / "p" / "2026-06-08_120000" / "full"
    dump_path = backup_dir / "database.sql.gz"
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert backup_run_result.errors == {}
    assert dump_path.read_bytes() == b"\x1f\x8bfake-dump"
    assert manifest["files"][0]["s3_key"] == "p/2026-06-08_120000/full/database.sql.gz"
    assert (
        manifest["files"][0]["sha256"]
        == hashlib.sha256(dump_path.read_bytes()).hexdigest()
    )
    assert not (tmp_path / "work" / "2026-06-08_120000").exists()


def test_backup_writes_local_copy_and_s3_when_both_are_configured(
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "backups"
    uploaded_keys: list[str] = []

    class RecordingS3:
        def __init__(self, **_: Any) -> None:
            pass

        def upload_file(self, file_path: Path, key: str) -> None:
            uploaded_keys.append(key)

        def upload_json(self, data: dict, key: str) -> None:
            uploaded_keys.append(key)

        def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
            return 0

    _backup_impl.run_backup(
        _options(tmp_path, local_dir=str(local_dir), s3_endpoint="http://s3"),
        s3_client_factory=RecordingS3,
        now=BACKUP_NOW,
    )

    assert "p/2026-06-08_120000/full/database.sql.gz" in uploaded_keys
    assert "p/2026-06-08_120000/full/manifest.json" in uploaded_keys
    assert (local_dir / "p/2026-06-08_120000/full/database.sql.gz").is_file()


def test_backup_requires_a_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="S3 endpoint or a local backup directory"):
        _backup_impl.run_backup(_options(tmp_path, local_dir=""))


def test_local_retention_uses_backup_directory_timestamps(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path, "p")
    now_dt = datetime.now(timezone.utc)
    old_name = (now_dt - timedelta(days=40)).strftime("%Y-%m-%d_%H%M%S")
    recent_name = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    for name in (old_name, recent_name):
        store.upload_json(
            {"timestamp": name, "type": "full"}, f"p/{name}/full/manifest.json"
        )
    unrelated_dir = tmp_path / "p" / "notes"
    unrelated_dir.mkdir()

    deleted_count = store.cleanup_old_backups("p", retention_days=30)

    assert deleted_count == 1
    assert store.list_backup_dates() == [recent_name]
    assert store.get_backup_manifest(recent_name) == {
        "timestamp": recent_name,
        "type": "full",
    }
    assert unrelated_dir.is_dir()


def test_local_store_rejects_keys_outside_the_root(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups", "p")

    with pytest.raises(ValueError, match="escapes"):
        store.upload_json({}, "../outside.json")


def test_db_list_reads_a_local_backup_directory(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path, "p")
    store.upload_json({"type": "full"}, "p/2026-06-08_120000/full/manifest.json")

    list_result = CliRunner().invoke(
        app, ["db", "list", "--local-dir", str(tmp_path), "--s3-prefix", "p"]
    )

    assert list_result.exit_code == 0
    assert "2026-06-08_120000" in list_result.stdout
    assert "full" in list_result.stdout
