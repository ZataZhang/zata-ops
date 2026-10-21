"""Bounded, read-only probes with separate observations and inventory metadata."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from zata_ops.inventory import ApplicationTarget

CheckStatus = Literal["passed", "failed", "unknown", "planned"]


@dataclass(frozen=True)
class CheckObservation:
    """One observation, including its method, time, and bounded evidence."""

    name: str
    status: CheckStatus
    reason: str
    method: str
    checked_at: str
    evidence: dict


@dataclass(frozen=True)
class InspectionOptions:
    """Resolved input and maximum seconds for each external probe."""

    target: ApplicationTarget
    timeout: int = 10
    dry_run: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, *redirect_arguments: object, **redirect_keywords: object
    ) -> None:
        # The configured URL alone is observed; redirect destinations are not probed.
        return None


def _observation(
    name: str, status: CheckStatus, reason: str, evidence: dict | None = None
) -> CheckObservation:
    return CheckObservation(
        name=name,
        status=status,
        reason=reason,
        method={
            "ssh": "ssh true",
            "runtime": "remote read-only status",
            "http": "HTTP HEAD",
        }[name],
        checked_at=datetime.now(timezone.utc).isoformat(),
        evidence=evidence or {},
    )


def _ssh_prefix(options: InspectionOptions) -> list[str] | None:
    server = options.target.server
    host_address = server.public_ip or server.private_ip
    ssh_config = server.ssh
    if not host_address or not ssh_config.user.strip() or not ssh_config.port:
        return None
    # Users are separate argv but OpenSSH still parses destination syntax.
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", ssh_config.user):
        return None
    ssh_argv = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={options.timeout}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-p",
        str(ssh_config.port),
        "-l",
        ssh_config.user,
    ]
    if ssh_config.identity_file:
        ssh_argv.extend(["-i", str(Path(ssh_config.identity_file).expanduser())])
    # -l keeps IPv6 addresses out of user@host ambiguity.
    ssh_argv.append(str(host_address))
    return ssh_argv


def _run_remote(
    ssh_argv: list[str], command: str, timeout: int
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ssh_argv, command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _check_ssh(
    options: InspectionOptions, ssh_argv: list[str] | None
) -> CheckObservation:
    if ssh_argv is None:
        return _observation("ssh", "unknown", "missing_or_invalid_ssh_configuration")
    if options.dry_run:
        return _observation("ssh", "planned", "no_connection_made")
    try:
        ssh_process = _run_remote(ssh_argv, "true", options.timeout)
    except subprocess.TimeoutExpired:
        return _observation("ssh", "failed", "timeout")
    except OSError:
        return _observation("ssh", "unknown", "ssh_unavailable")
    return _observation(
        "ssh",
        "passed" if ssh_process.returncode == 0 else "failed",
        "connected"
        if ssh_process.returncode == 0
        else "connection_or_authentication_failed",
        {"exit_code": ssh_process.returncode},
    )


def _check_runtime(
    options: InspectionOptions, ssh_context: tuple[list[str] | None, CheckObservation]
) -> CheckObservation:
    ssh_argv, ssh_observation = ssh_context
    application = options.target.application
    if application.deployment_type not in {"docker-compose", "systemd"}:
        return _observation("runtime", "unknown", "unsupported_deployment_type")
    if not application.deployment_id.strip():
        return _observation("runtime", "unknown", "missing_deployment_id")
    if ssh_argv is None or ssh_observation.status not in {"passed", "planned"}:
        return _observation("runtime", "unknown", "ssh_not_verified")
    if options.dry_run:
        return _observation("runtime", "planned", "no_connection_made")
    if application.deployment_type == "systemd":
        remote_command = shlex.join(
            ["systemctl", "is-active", "--", application.deployment_id]
        )
    else:
        remote_command = shlex.join(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={application.deployment_id}",
                "--format",
                "{{json .}}",
            ]
        )
    try:
        runtime_process = _run_remote(ssh_argv, remote_command, options.timeout)
    except subprocess.TimeoutExpired:
        return _observation("runtime", "unknown", "timeout")
    except OSError:
        return _observation("runtime", "unknown", "ssh_unavailable")
    if application.deployment_type == "systemd":
        unit_state = runtime_process.stdout.strip()
        if runtime_process.returncode == 0 and unit_state == "active":
            return _observation("runtime", "passed", "unit_active")
        if runtime_process.returncode in {3, 4} and unit_state in {
            "inactive",
            "failed",
            "unknown",
        }:
            return _observation("runtime", "failed", "unit_not_active")
        return _observation("runtime", "unknown", "status_command_failed")
    if runtime_process.returncode != 0:
        return _observation(
            "runtime",
            "unknown",
            "status_command_failed",
            {"exit_code": runtime_process.returncode},
        )
    try:
        containers = [
            json.loads(line)
            for line in runtime_process.stdout.splitlines()
            if line.strip()
        ]
        if any(
            not isinstance(container, dict)
            or container.get("State")
            not in {
                "created",
                "restarting",
                "running",
                "removing",
                "paused",
                "exited",
                "dead",
            }
            or not isinstance(container.get("Status", ""), str)
            for container in containers
        ):
            raise ValueError("invalid container status")
    except (ValueError, TypeError):
        return _observation("runtime", "unknown", "invalid_status_output")
    if not containers:
        return _observation("runtime", "failed", "no_matching_containers")
    state_counts: dict[str, int] = {}
    for container in containers:
        container_state = container["State"]
        state_counts[container_state] = state_counts.get(container_state, 0) + 1
    all_running = all(container["State"] == "running" for container in containers)
    # A running container may still have a failing Docker healthcheck.
    unhealthy_count = sum(
        "(unhealthy)" in container.get("Status", "") for container in containers
    )
    starting_count = sum(
        "(health: starting)" in container.get("Status", "") for container in containers
    )
    evidence = {
        "container_count": len(containers),
        "state_counts": state_counts,
        "unhealthy_count": unhealthy_count,
    }
    if not all_running or unhealthy_count:
        return _observation("runtime", "failed", "containers_not_ready", evidence)
    if starting_count:
        return _observation("runtime", "unknown", "healthcheck_starting", evidence)
    return _observation("runtime", "passed", "observed_containers_running", evidence)


def _check_http(options: InspectionOptions) -> CheckObservation:
    application_url = options.target.application.url
    if not application_url:
        return _observation("http", "unknown", "missing_url")
    try:
        parsed_url = urlsplit(application_url)
        valid_url = (
            parsed_url.scheme in {"http", "https"}
            and bool(parsed_url.hostname)
            and not parsed_url.username
            and not parsed_url.password
        )
        parsed_url.port  # Validate malformed ports before urllib can echo an error.
    except ValueError:
        valid_url = False
    if not valid_url:
        return _observation("http", "unknown", "invalid_url")
    if options.dry_run:
        return _observation("http", "planned", "no_request_made")
    try:
        http_request = Request(
            application_url, method="HEAD", headers={"User-Agent": "zata-ops-inspect"}
        )
        with build_opener(_NoRedirect).open(
            http_request, timeout=options.timeout
        ) as http_response:
            status_code = http_response.status
    except HTTPError as http_error:
        status_code = http_error.code
        http_error.close()
    except (OSError, URLError, ValueError, HTTPException):
        return _observation("http", "failed", "request_failed")
    evidence = {
        "status_code": status_code,
        "vantage_point": "local_machine",
        "redirects_followed": False,
    }
    if status_code in {405, 501}:
        return _observation("http", "unknown", "head_not_supported", evidence)
    return _observation(
        "http",
        "passed" if 200 <= status_code < 400 else "failed",
        "endpoint_responded" if 200 <= status_code < 400 else "http_error_status",
        evidence,
    )


def inspect_application(options: InspectionOptions) -> tuple[dict, int]:
    """Observe independent SSH, runtime and local HTTP checks.

    Args:
        options: Resolved deployment, timeout and preview flag.

    Returns:
        Versioned JSON-compatible report and exit code: 0 for all passed or
        preview, 1 for observed failure, 3 for incomplete observations.
    """
    ssh_argv = _ssh_prefix(options)
    ssh_observation = _check_ssh(options, ssh_argv)
    observations = [
        ssh_observation,
        _check_runtime(options, (ssh_argv, ssh_observation)),
        _check_http(options),
    ]
    statuses = {observation.status for observation in observations}
    if options.dry_run:
        overall_status, exit_code = "planned", 0
    elif "failed" in statuses:
        overall_status, exit_code = "failed", 1
    elif "unknown" in statuses:
        overall_status, exit_code = "incomplete", 3
    else:
        overall_status, exit_code = "passed", 0
    return {
        "schema_version": 1,
        "operation": "app.inspect",
        "status": overall_status,
        "target": {
            "server": options.target.server.name,
            "application": options.target.application.name,
            "deployment_type": options.target.application.deployment_type,
        },
        "inventory": {
            "applications_inventory_status": options.target.server.applications_inventory_status,
            "application_verified_at": options.target.application.verified_at,
        },
        "checks": [asdict(observation) for observation in observations],
    }, exit_code
