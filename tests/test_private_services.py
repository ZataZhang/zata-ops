"""Validate service registration, metadata privacy and credential association."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values
from test_private_secrets import credential_dir as shared_credential_fixture

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/diagnostics"


@pytest.fixture
def credential_dir():
    yield from shared_credential_fixture.__wrapped__()


@pytest.fixture
def service_module(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_DIR))
    return importlib.import_module("private_services")


def _register(monkeypatch, service_module, credential_dir, *, confirm="y"):
    responses = iter(
        [
            "主数据库",
            "",
            "",
            "",
            "db.example.com",
            "",
            "app",
            "app_user",
            "测试服务",
            confirm,
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        service_module, "read_hidden_secret", lambda prompt: "synthetic-test-token"
    )
    service_module.add_service(credential_dir, service_module.ServicesConfig())


def test_registration_and_real_query_and_run(
    monkeypatch, service_module, credential_dir
):
    _register(monkeypatch, service_module, credential_dir)
    metadata_path = credential_dir / "services.toml"
    metadata_text = metadata_path.read_text(encoding="utf-8")
    assert "synthetic-test-token" not in metadata_text
    service = tomllib.loads(metadata_text)["services"][0]
    assert service["port"] == 5432
    assert service["username"] == "app_user"
    assert (
        dotenv_values(credential_dir / "secrets.env", interpolate=False)[
            service["password_env"]
        ]
        == "synthetic-test-token"
    )
    assert metadata_path.stat().st_mode & 0o777 == 0o600
    command_prefix = [
        sys.executable,
        str(SCRIPT_DIR / "private_services.py"),
        "--private-dir",
        str(credential_dir),
    ]
    queried = subprocess.run(
        [*command_prefix, "show", "主数据库"], capture_output=True, text=True
    )
    assert queried.returncode == 0
    assert json.loads(queried.stdout)[0]["password_configured"] is True
    assert "synthetic-test-token" not in queried.stdout + queried.stderr
    executed = subprocess.run(
        [
            *command_prefix,
            "run",
            "--password-env",
            "PGPASSWORD",
            "主数据库",
            "--",
            sys.executable,
            "-c",
            "import os; assert os.environ['PGPASSWORD']=='synthetic-test-token'; print(os.environ['PGPASSWORD'])",
        ],
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0
    assert "synthetic-test-token" not in executed.stdout + executed.stderr


def test_cancel_leaves_no_configuration(monkeypatch, service_module, credential_dir):
    _register(monkeypatch, service_module, credential_dir, confirm="n")
    assert not (credential_dir / "services.toml").exists()
    assert not (credential_dir / "secrets.env").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com",
        "https://example.com?token=abc",
        "https://example.com#token",
        "not-a-url",
    ],
)
def test_reject_credential_urls(service_module, url):
    with pytest.raises(ValueError):
        service_module.ServiceConfig.validate_public_url(url)


def test_existing_services_preserved_and_duplicate_rejected(
    monkeypatch, service_module, credential_dir
):
    _register(monkeypatch, service_module, credential_dir)
    saved_inventory = service_module.load_services(credential_dir / "services.toml")
    original_text = (credential_dir / "secrets.env").read_text(encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "主数据库")
    with pytest.raises(ValueError):
        service_module.add_service(credential_dir, saved_inventory)
    assert (credential_dir / "secrets.env").read_text(encoding="utf-8") == original_text
