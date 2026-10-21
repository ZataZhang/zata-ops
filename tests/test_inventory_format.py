"""Verify canonical hierarchy, lossless strings and safe private-file writes."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from zata_ops.inventory import InventoryConfig
from test_private_secrets import credential_dir as shared_credential_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/diagnostics/format_inventory.py"
EXAMPLE_PATH = REPO_ROOT / "config/servers.example.toml"


@pytest.fixture
def formatter(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_PATH.parent))
    return importlib.import_module("format_inventory")


@pytest.fixture
def private_dir():
    yield from shared_credential_fixture.__wrapped__()


def _run_formatter(path: Path, *arguments: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(path), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        timeout=20,
    )


def test_example_is_canonical():
    checked = _run_formatter(EXAMPLE_PATH, "--example")
    assert checked.returncode == 0


@pytest.mark.parametrize(
    "notes",
    [
        '中文备注与 "引号"、反斜杠 \\、真实换行\n下一行、制表符\t' * 6,
        "x" * 66 + " " * 5 + "value" * 25,
        '结束的反斜杠\\与三引号"""' * 10,
    ],
)
def test_roundtrip_is_lossless_and_idempotent(formatter, notes):
    inventory_payload = tomllib.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    inventory_payload["servers"][0]["notes"] = notes
    inventory_payload["servers"][0]["applications"][0]["notes"] = notes
    inventory = InventoryConfig.model_validate_json(json.dumps(inventory_payload))
    formatted_text = formatter.render_inventory(inventory)
    assert tomllib.loads(formatted_text) == inventory_payload
    reparsed = InventoryConfig.model_validate_json(
        json.dumps(tomllib.loads(formatted_text))
    )
    assert formatter.render_inventory(reparsed) == formatted_text
    assert "  [servers.ssh]" in formatted_text
    assert "  [[servers.applications]]" in formatted_text
    assert "    name = " in formatted_text


def test_empty_applications_belong_to_correct_server(formatter):
    inventory_payload = tomllib.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    second_server = json.loads(json.dumps(inventory_payload["servers"][0]))
    inventory_payload["servers"][0]["applications"] = []
    second_server["name"] = "second-server"
    inventory_payload["servers"].append(second_server)
    inventory = InventoryConfig.model_validate_json(json.dumps(inventory_payload))
    formatted_text = formatter.render_inventory(inventory)
    assert tomllib.loads(formatted_text) == inventory_payload
    assert formatted_text.index("  applications = []") < formatted_text.index(
        "  [servers.ssh]"
    )


def test_private_write_backs_up_and_preserves_values(private_dir):
    inventory_path = private_dir / "servers.toml"
    original_text = EXAMPLE_PATH.read_text(encoding="utf-8").replace(
        '  name = "example-server"', 'name = "private-fixture-name"'
    )
    inventory_path.write_text(original_text, encoding="utf-8")
    inventory_path.chmod(0o600)
    check_result = _run_formatter(inventory_path)
    assert check_result.returncode == 1
    assert inventory_path.read_text(encoding="utf-8") == original_text
    assert not (private_dir / "backups").exists()
    write_result = _run_formatter(inventory_path, "--write")
    assert write_result.returncode == 0
    assert "private-fixture-name" not in write_result.stdout + write_result.stderr
    assert tomllib.loads(inventory_path.read_text(encoding="utf-8")) == tomllib.loads(
        original_text
    )
    assert inventory_path.stat().st_mode & 0o777 == 0o600
    archives = list((private_dir / "backups").glob("private-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        archived = archive.extractfile("servers.toml")
        assert archived is not None
        assert archived.read().decode("utf-8") == original_text
    assert _run_formatter(inventory_path, "--write").returncode == 0
    assert len(list((private_dir / "backups").glob("private-*.tar.gz"))) == 1


def test_backup_failure_leaves_original_untouched(formatter, monkeypatch, private_dir):
    inventory_path = private_dir / "servers.toml"
    original_text = EXAMPLE_PATH.read_text(encoding="utf-8").replace(
        '  name = "example-server"', 'name = "private-fixture-name"'
    )
    inventory_path.write_text(original_text, encoding="utf-8")

    def failing_backup(private_path):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(formatter, "create_backup", failing_backup)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), str(inventory_path), "--write"])
    assert formatter.main() == 2
    assert inventory_path.read_text(encoding="utf-8") == original_text


def test_example_flag_cannot_write_private_or_arbitrary_files(private_dir):
    inventory_path = private_dir / "servers.toml"
    original_text = EXAMPLE_PATH.read_text(encoding="utf-8")
    inventory_path.write_text(original_text, encoding="utf-8")
    assert _run_formatter(inventory_path, "--example", "--write").returncode == 2
    assert inventory_path.read_text(encoding="utf-8") == original_text


def test_symlink_rejected(private_dir):
    inventory_path = private_dir / "servers.toml"
    inventory_path.symlink_to(EXAMPLE_PATH)
    assert _run_formatter(inventory_path, "--write").returncode == 2


def test_invalid_inventory_values_not_echoed(private_dir):
    inventory_path = private_dir / "servers.toml"
    inventory_path.write_text('schema_version = "PRIVATE_SENTINEL"', encoding="utf-8")
    rejected = _run_formatter(inventory_path, "--write")
    assert rejected.returncode == 2
    assert "PRIVATE_SENTINEL" not in rejected.stdout + rejected.stderr
