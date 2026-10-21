"""Inventory and probe boundary tests, including a real CLI subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zata_ops.app import _inspect
from zata_ops.cli import app
from zata_ops.inventory import load_inventory, resolve_application

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "config/servers.example.toml"


@pytest.fixture
def inventory_path(tmp_path: Path) -> Path:
    """Create a fictional, independently editable inventory."""
    inventory_text = EXAMPLE_PATH.read_text(encoding="utf-8")
    configured_text = inventory_text.replace('user = ""', 'user = "deploy"').replace(
        'port = ""', "port = 22"
    )
    target_path = tmp_path / "inventory.toml"
    target_path.write_text(configured_text, encoding="utf-8")
    return target_path


def _invoke(inventory_path: Path, *arguments: str):
    return CliRunner().invoke(
        app,
        [
            "app",
            "inspect",
            "example-app",
            "--inventory",
            str(inventory_path),
            *arguments,
        ],
    )


def test_preview_performs_no_network(inventory_path, monkeypatch):
    def unexpected_network(*arguments, **keywords):
        pytest.fail("preview attempted network access")

    monkeypatch.setattr(_inspect, "_run_remote", unexpected_network)
    monkeypatch.setattr(_inspect, "build_opener", unexpected_network)
    preview_result = _invoke(inventory_path, "--dry-run")
    assert preview_result.exit_code == 0
    preview_report = json.loads(preview_result.stdout)
    assert preview_report["status"] == "planned"
    assert {check["status"] for check in preview_report["checks"]} == {"planned"}
    assert preview_report["inventory"]["updated_at"] == "2026-01-01"
    assert "identity_file" not in preview_result.stdout


@pytest.mark.parametrize(
    "inventory_text",
    ["invalid TOML", 'schema_version = "SECRET_VALUE"', "updated_at = 2026-01-01"],
)
def test_invalid_inventory_is_json_and_redacted(tmp_path, inventory_text):
    invalid_path = tmp_path / "invalid.toml"
    invalid_path.write_text(inventory_text, encoding="utf-8")
    invalid_result = _invoke(invalid_path)
    assert invalid_result.exit_code == 2
    assert json.loads(invalid_result.stdout)["reason"] == "invalid_inventory"
    assert "SECRET_VALUE" not in invalid_result.stdout


def test_ambiguous_application_requires_server(inventory_path):
    inventory_text = inventory_path.read_text(encoding="utf-8")
    extra_server = inventory_text[inventory_text.index("[[servers]]") :].replace(
        'name = "example-server"', 'name = "second-server"'
    )
    inventory_path.write_text(inventory_text + "\n" + extra_server, encoding="utf-8")
    ambiguous_result = _invoke(inventory_path, "--dry-run")
    assert ambiguous_result.exit_code == 2
    assert json.loads(ambiguous_result.stdout)["reason"] == "target_ambiguous"
    assert (
        _invoke(inventory_path, "--server", "second-server", "--dry-run").exit_code == 0
    )


def test_missing_target_does_not_fall_back(inventory_path):
    missing_result = _invoke(inventory_path, "--server", "does-not-exist")
    assert missing_result.exit_code == 2
    assert json.loads(missing_result.stdout)["reason"] == "target_not_found"


def test_unknown_configuration_is_not_success(inventory_path, monkeypatch):
    inventory_text = (
        inventory_path.read_text(encoding="utf-8")
        .replace('user = "deploy"', 'user = ""')
        .replace('url = "https://app.example.com"', 'url = ""')
    )
    inventory_path.write_text(inventory_text, encoding="utf-8")
    unknown_result = _invoke(inventory_path)
    assert unknown_result.exit_code == 3
    unknown_report = json.loads(unknown_result.stdout)
    assert unknown_report["status"] == "incomplete"
    assert {check["status"] for check in unknown_report["checks"]} == {"unknown"}


def test_ssh_failure_still_checks_http(inventory_path, monkeypatch):
    monkeypatch.setattr(
        _inspect,
        "_run_remote",
        lambda *args: subprocess.CompletedProcess(
            [], 255, stdout="", stderr="SECRET_VALUE"
        ),
    )
    http_calls = []

    def record_http(options):
        http_calls.append(options)
        return _inspect._observation("http", "passed", "endpoint_responded")

    monkeypatch.setattr(_inspect, "_check_http", record_http)
    failed_result = _invoke(inventory_path)
    assert failed_result.exit_code == 1
    failed_checks = {
        check["name"]: check for check in json.loads(failed_result.stdout)["checks"]
    }
    assert failed_checks["runtime"]["status"] == "unknown"
    assert failed_checks["http"]["status"] == "passed"
    assert len(http_calls) == 1
    assert "SECRET_VALUE" not in failed_result.stdout


@pytest.mark.parametrize(
    "runtime_case",
    [
        ('{"State":"running","Status":"Up 1 hour (healthy)"}', "passed"),
        ('{"State":"running","Status":"Up 1 hour (unhealthy)"}', "failed"),
        ('{"State":"exited","Status":"Exited"}', "failed"),
        ('{"State":"running","Status":"Up 1 hour (health: starting)"}', "unknown"),
        ("", "failed"),
        ("not json", "unknown"),
        ('{"State":[]}', "unknown"),
    ],
)
def test_runtime_evidence_does_not_claim_unverified_health(
    inventory_path, monkeypatch, runtime_case
):
    remote_output, expected_status = runtime_case
    application_target = resolve_application(
        load_inventory(inventory_path), "example-app", None
    )
    inspection_options = _inspect.InspectionOptions(target=application_target)
    monkeypatch.setattr(
        _inspect,
        "_run_remote",
        lambda *args: subprocess.CompletedProcess(
            [], 0, stdout=remote_output, stderr=""
        ),
    )
    runtime_observation = _inspect._check_runtime(
        inspection_options,
        (["ssh"], _inspect._observation("ssh", "passed", "connected")),
    )
    assert runtime_observation.status == expected_status


def test_timeout_has_bounded_unknown_runtime(inventory_path, monkeypatch):
    application_target = resolve_application(
        load_inventory(inventory_path), "example-app", None
    )

    def timed_out(*arguments):
        raise subprocess.TimeoutExpired("ssh", 10)

    monkeypatch.setattr(_inspect, "_run_remote", timed_out)
    options = _inspect.InspectionOptions(target=application_target)
    assert _inspect._check_ssh(options, ["ssh"]).status == "failed"
    assert (
        _inspect._check_runtime(
            options, (["ssh"], _inspect._observation("ssh", "passed", "connected"))
        ).status
        == "unknown"
    )


def test_ssh_uses_inventory_port_identity_and_noninteractive_options(inventory_path):
    application_target = resolve_application(
        load_inventory(inventory_path), "example-app", None
    )
    application_target.server.ssh.port = 2222
    application_target.server.ssh.identity_file = "~/fictional-key"
    ssh_argv = _inspect._ssh_prefix(
        _inspect.InspectionOptions(target=application_target)
    )
    assert ssh_argv[ssh_argv.index("-p") + 1] == "2222"
    assert ssh_argv[ssh_argv.index("-i") + 1] == str(
        Path("~/fictional-key").expanduser()
    )
    assert "BatchMode=yes" in ssh_argv
    assert "StrictHostKeyChecking=yes" in ssh_argv
    assert "ClearAllForwardings=yes" in ssh_argv


@pytest.mark.parametrize(
    "systemd_case",
    [(0, "active", "passed"), (3, "inactive", "failed"), (255, "", "unknown")],
)
def test_systemd_observes_active_state(inventory_path, monkeypatch, systemd_case):
    exit_code, unit_state, expected = systemd_case
    application_target = resolve_application(
        load_inventory(inventory_path), "example-app", None
    )
    application_target.application.deployment_type = "systemd"
    application_target.application.deployment_id = "example.service; touch /tmp/never"
    commands = []

    def remote_status(ssh_argv, command, timeout):
        commands.append(command)
        return subprocess.CompletedProcess([], exit_code, stdout=unit_state, stderr="")

    monkeypatch.setattr(_inspect, "_run_remote", remote_status)
    observation = _inspect._check_runtime(
        _inspect.InspectionOptions(target=application_target),
        (["ssh"], _inspect._observation("ssh", "passed", "connected")),
    )
    assert observation.status == expected
    assert commands == ["systemctl is-active -- 'example.service; touch /tmp/never'"]


class _HeadHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(int(self.path.strip("/")))
        if self.path == "/302":
            self.send_header("Location", "/500")
        self.end_headers()

    def log_message(self, *arguments):
        pass


@pytest.fixture
def http_origin():
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _HeadHandler)
    server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    server_thread.start()
    yield f"http://127.0.0.1:{http_server.server_port}"
    http_server.shutdown()
    server_thread.join(timeout=2)
    http_server.server_close()


@pytest.mark.parametrize(
    "http_case", [(200, "passed"), (302, "passed"), (500, "failed"), (405, "unknown")]
)
def test_http_observes_real_response_without_following_redirect(
    inventory_path, http_origin, http_case
):
    status_code, expected = http_case
    application_target = resolve_application(
        load_inventory(inventory_path), "example-app", None
    )
    application_target.application.url = f"{http_origin}/{status_code}"
    observation = _inspect._check_http(
        _inspect.InspectionOptions(target=application_target)
    )
    assert observation.status == expected
    assert observation.evidence["status_code"] == status_code


def test_real_cli_with_fake_ssh_and_local_http(inventory_path, tmp_path, http_origin):
    inventory_text = inventory_path.read_text(encoding="utf-8").replace(
        "https://app.example.com", f"{http_origin}/200"
    )
    inventory_path.write_text(inventory_text, encoding="utf-8")
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        '#!/bin/sh\ncase "$*" in\n  *\'docker ps\'*) printf \'%s\\n\' \'{"State":"running","Status":"Up 1 hour"}\';;\nesac\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)
    cli_environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    cli_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "zata_ops",
            "app",
            "inspect",
            "example-app",
            "--inventory",
            str(inventory_path),
        ],
        env=cli_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert cli_process.returncode == 0, cli_process.stderr
    report = json.loads(cli_process.stdout)
    assert report["status"] == "passed"
    assert {check["status"] for check in report["checks"]} == {"passed"}
    assert all(check["checked_at"] for check in report["checks"])
    assert report["checks"][1]["evidence"]["container_count"] == 1
