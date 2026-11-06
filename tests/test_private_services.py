"""Validate service registration, metadata privacy and credential association."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tomllib
from datetime import date, timedelta
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


def _register(
    monkeypatch,
    service_module,
    credential_dir,
    *,
    confirm="y",
    name="主数据库",
    lifecycle=("", "", "", ""),
    inventory=None,
    hidden_secret=lambda prompt: "synthetic-test-token",
):
    responses = iter(
        [
            name,
            "",
            "",
            "",
            "db.example.com",
            "",
            "app",
            "app_user",
            "测试服务",
            *lifecycle,
            confirm,
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(service_module, "read_hidden_secret", hidden_secret)
    service_module.add_service(
        credential_dir, inventory or service_module.ServicesConfig()
    )


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


def test_registration_defaults_created_at_to_today(
    monkeypatch, service_module, credential_dir
):
    _register(monkeypatch, service_module, credential_dir)
    service = tomllib.loads(
        (credential_dir / "services.toml").read_text(encoding="utf-8")
    )["services"][0]
    assert service["created_at"] == date.today().isoformat()
    assert service["scope"] == ""
    assert service["expires_at"] == ""


def test_link_existing_token_with_lifecycle_metadata(
    monkeypatch, service_module, credential_dir
):
    service_module.store_secret(
        credential_dir / "secrets.env", "GRAFANA_TEST_TOKEN", "synthetic-grafana-token"
    )
    original_secrets = (credential_dir / "secrets.env").read_text(encoding="utf-8")

    def _no_hidden_input(prompt):
        raise AssertionError("linking an existing credential must not prompt for it")

    _register(
        monkeypatch,
        service_module,
        credential_dir,
        name="grafana-test",
        lifecycle=("Viewer", "2026-11-06", "2027-02-04", "GRAFANA_TEST_TOKEN"),
        hidden_secret=_no_hidden_input,
    )
    service = tomllib.loads(
        (credential_dir / "services.toml").read_text(encoding="utf-8")
    )["services"][0]
    assert service["password_env"] == "GRAFANA_TEST_TOKEN"
    assert (service["scope"], service["created_at"], service["expires_at"]) == (
        "Viewer",
        "2026-11-06",
        "2027-02-04",
    )
    assert (credential_dir / "secrets.env").read_text(encoding="utf-8") == (
        original_secrets
    )


@pytest.mark.parametrize(
    ("reference", "seed_other_service"),
    [("GRAFANA_MISSING_TOKEN", False), ("GRAFANA_TEST_TOKEN", True)],
)
def test_link_rejects_unknown_or_already_linked_credential(
    monkeypatch, service_module, credential_dir, reference, seed_other_service
):
    service_module.store_secret(
        credential_dir / "secrets.env", "GRAFANA_TEST_TOKEN", "synthetic-grafana-token"
    )
    inventory = service_module.ServicesConfig()
    if seed_other_service:
        _register(
            monkeypatch,
            service_module,
            credential_dir,
            name="grafana-a",
            lifecycle=("", "", "", "GRAFANA_TEST_TOKEN"),
        )
        inventory = service_module.load_services(credential_dir / "services.toml")
    metadata_before = (
        (credential_dir / "services.toml").read_text(encoding="utf-8")
        if seed_other_service
        else None
    )
    with pytest.raises(ValueError):
        _register(
            monkeypatch,
            service_module,
            credential_dir,
            name="grafana-b",
            lifecycle=("", "", "", reference),
            inventory=inventory,
        )
    if seed_other_service:
        assert (credential_dir / "services.toml").read_text(
            encoding="utf-8"
        ) == metadata_before
    else:
        assert not (credential_dir / "services.toml").exists()


@pytest.mark.parametrize("bad_date", ["2027-2-4", "2027-02-30", "tomorrow"])
def test_reject_invalid_lifecycle_dates(service_module, bad_date):
    with pytest.raises(ValueError):
        service_module.ServiceConfig(
            name="x",
            server="",
            type="http",
            host="example.com",
            port="",
            database="",
            username="",
            password_env="X_TOKEN",
            notes="",
            expires_at=bad_date,
        )


def test_legacy_records_without_lifecycle_fields_still_parse(service_module):
    legacy_text = (
        "schema_version = 1\n\n[[services]]\n"
        'name = "old-db"\nserver = ""\ntype = "postgresql"\nhost = "db"\n'
        'port = 5432\ndatabase = "app"\nusername = "u"\nurl = ""\n'
        'password_env = "OLD_DB_PASSWORD"\nnotes = ""\n'
    )
    legacy_record = service_module.parse_services_config(legacy_text).services[0]
    assert (
        legacy_record.scope,
        legacy_record.created_at,
        legacy_record.expires_at,
    ) == (
        "",
        "",
        "",
    )


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        ("", ("none", None)),
        ("2026-12-06", ("expiring", 30)),
        ("2026-12-07", ("ok", 31)),
        ("2026-11-06", ("expiring", 0)),
        ("2026-11-05", ("expired", -1)),
    ],
)
def test_expiry_classification_boundaries(service_module, expires_at, expected):
    from zata_ops.services import credential_expiry

    record = service_module.ServiceConfig(
        name="x",
        server="",
        type="http",
        host="example.com",
        port="",
        database="",
        username="",
        password_env="X_TOKEN",
        notes="",
        expires_at=expires_at,
    )
    assert credential_expiry(record, date(2026, 11, 6)) == expected


def test_list_warns_on_expiring_credential_and_keeps_json_parseable(
    monkeypatch, service_module, credential_dir
):
    soon = (date.today() + timedelta(days=5)).isoformat()
    _register(
        monkeypatch,
        service_module,
        credential_dir,
        name="grafana-soon",
        lifecycle=("Viewer", "", soon, ""),
    )
    listed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "private_services.py"),
            "--private-dir",
            str(credential_dir),
            "list",
        ],
        capture_output=True,
        text=True,
    )
    assert listed.returncode == 0
    summary = json.loads(listed.stdout)[0]
    assert (summary["expiry_status"], summary["days_until_expiry"]) == ("expiring", 5)
    assert "grafana-soon" in listed.stderr and "5 天后" in listed.stderr
    assert "synthetic-test-token" not in listed.stdout + listed.stderr
