"""Docker and gVisor host operations used by the OpenSandbox installer."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from configuration import NETWORK_NAME

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
DOCKER_CONFIG_PATH = Path("/etc/docker/daemon.json")
DOCKER_CONFIG_BACKUP_PATH = Path("/var/backups/zata-ops/opensandbox")
GVISOR_KEY_URL = "https://gvisor.dev/archive.key"
GVISOR_KEYRING_PATH = Path("/usr/share/keyrings/gvisor-archive-keyring.gpg")
GVISOR_SOURCE_PATH = Path("/etc/apt/sources.list.d/gvisor.list")
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/opensandbox.service")
RUNTIME_ARGUMENTS = ["--platform=systrap", "--network=sandbox", "--overlay2=none"]
PREVIOUS_HOST_NETWORK_ARGUMENTS = [
    "--platform=systrap",
    "--network=host",
    "--overlay2=none",
]


class InstallFailure(RuntimeError):
    """A safe deployment error that contains no configuration or secret values."""


def run_capture(command: list[str], *, timeout_seconds: int = 30) -> str:
    """Run a local command and return stdout without echoing error details.

    Args:
        command: Argument vector executed without a shell.
        timeout_seconds: Maximum command runtime.

    Returns:
        Standard output text.

    Raises:
        InstallFailure: The command failed or exceeded its timeout.
    """
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as command_error:
        raise InstallFailure(
            f"command_failed:{Path(command[0]).name}"
        ) from command_error
    return completed.stdout.strip()


def run_visible(command: list[str], *, timeout_seconds: int = 900) -> None:
    """Run an installation command with progress visible to the operator.

    Args:
        command: Argument vector executed without a shell.
        timeout_seconds: Maximum command runtime.

    Raises:
        InstallFailure: The command failed or exceeded its timeout.
    """
    environment = os.environ.copy()
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    environment["NEEDRESTART_MODE"] = "l"
    try:
        subprocess.run(
            command,
            check=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as command_error:
        raise InstallFailure(
            f"command_failed:{Path(command[0]).name}"
        ) from command_error


def read_docker_runtime_state() -> dict:
    """Read Docker PID, restart count, default runtime and running container states."""
    docker_info = json.loads(run_capture([*DOCKER, "info", "--format", "{{json .}}"]))
    systemd_properties = run_capture(
        ["systemctl", "show", "docker", "-p", "MainPID", "-p", "NRestarts"]
    )
    property_values = dict(
        line.split("=", 1) for line in systemd_properties.splitlines() if "=" in line
    )
    running_containers = run_capture(
        [*DOCKER, "ps", "--format", "{{.Names}}|{{.State}}"]
    )
    return {
        "main_pid": property_values.get("MainPID"),
        "restart_count": property_values.get("NRestarts"),
        "default_runtime": docker_info.get("DefaultRuntime"),
        "container_states": sorted(running_containers.splitlines()),
    }


def write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    """Replace one root-owned file atomically with explicit permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary_path = path.with_name(f".{path.name}.new-{os.getpid()}")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def backup_file(path: Path) -> Path | None:
    """Create a private timestamped backup before replacing an existing system file."""
    if not path.exists():
        return None
    backup_directory = DOCKER_CONFIG_BACKUP_PATH
    backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_directory.chmod(0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / f"{path.name}.{timestamp}.{os.getpid()}.bak"
    shutil.copyfile(path, backup_path)
    backup_path.chmod(0o600)
    return backup_path


def ensure_gvisor_repository() -> str:
    """Install the official signed gVisor apt source and the exact selected package.

    Returns:
        The installed runsc package version.
    """
    run_visible(["apt-get", "update"], timeout_seconds=600)
    run_visible(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "ca-certificates",
            "gnupg",
            "python3.12-venv",
        ],
        timeout_seconds=600,
    )
    try:
        with urllib.request.urlopen(GVISOR_KEY_URL, timeout=30) as key_response:
            signing_key = key_response.read(1024 * 1024 + 1)
    except OSError as key_error:
        raise InstallFailure("official_gvisor_signing_key_unavailable") from key_error
    if not signing_key or len(signing_key) > 1024 * 1024:
        raise InstallFailure("official_gvisor_signing_key_invalid_size")
    dearmored_key = subprocess.run(
        ["gpg", "--dearmor"],
        input=signing_key,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if dearmored_key.returncode != 0 or not dearmored_key.stdout:
        raise InstallFailure("official_gvisor_signing_key_invalid")
    if (
        not GVISOR_KEYRING_PATH.exists()
        or GVISOR_KEYRING_PATH.read_bytes() != dearmored_key.stdout
    ):
        backup_file(GVISOR_KEYRING_PATH)
        write_atomic(GVISOR_KEYRING_PATH, dearmored_key.stdout, mode=0o644)
    source_contents = (
        "deb [arch=amd64 signed-by=/usr/share/keyrings/"
        "gvisor-archive-keyring.gpg] https://storage.googleapis.com/"
        "gvisor/releases release main\n"
    )
    if (
        not GVISOR_SOURCE_PATH.exists()
        or GVISOR_SOURCE_PATH.read_text(encoding="utf-8") != source_contents
    ):
        backup_file(GVISOR_SOURCE_PATH)
        write_atomic(GVISOR_SOURCE_PATH, source_contents.encode("utf-8"), mode=0o644)
    run_visible(["apt-get", "update"], timeout_seconds=600)
    repository_policy = run_capture(["apt-cache", "policy", "runsc"])
    candidate_match = re.search(
        r"^  Candidate: (\S+)$", repository_policy, re.MULTILINE
    )
    if candidate_match is None or candidate_match.group(1) == "(none)":
        raise InstallFailure("official_gvisor_runsc_package_has_no_candidate")
    package_version = candidate_match.group(1)
    package_lines = repository_policy.splitlines()
    candidate_line_index = next(
        (
            index
            for index, line in enumerate(package_lines)
            if re.match(rf"^\s*(?:\*+\s*)?{re.escape(package_version)}\s", line)
        ),
        None,
    )
    if candidate_line_index is None or not any(
        "storage.googleapis.com/gvisor/releases" in line
        for line in package_lines[candidate_line_index : candidate_line_index + 4]
    ):
        raise InstallFailure("official_gvisor_repository_is_not_runsc_candidate")
    print(f"Installing signed gVisor runsc package {package_version}.", flush=True)
    run_visible(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            f"runsc={package_version}",
        ],
        timeout_seconds=900,
    )
    runsc_path = shutil.which("runsc")
    if runsc_path is None:
        raise InstallFailure("runsc_binary_not_installed")
    run_capture([runsc_path, "--version"])
    return package_version


def merge_runtime_policy(docker_config: dict, runsc_path: str) -> dict:
    """Add gVisor without changing Docker's other runtimes or default runtime.

    Args:
        docker_config: Existing Docker daemon JSON object.
        runsc_path: Resolved executable installed from the official package.

    Returns:
        A copied configuration with the reviewed runsc arguments.

    Raises:
        InstallFailure: Existing Docker settings conflict with this pilot profile.
    """
    merged_config = json.loads(json.dumps(docker_config))
    runtime_map = merged_config.setdefault("runtimes", {})
    if not isinstance(runtime_map, dict):
        raise InstallFailure("docker_runtimes_config_must_be_object")
    runsc_config = runtime_map.setdefault("runsc", {})
    if not isinstance(runsc_config, dict):
        raise InstallFailure("existing_runsc_runtime_config_conflicts")
    configured_path = runsc_config.get("path")
    if configured_path and shutil.which(configured_path) != runsc_path:
        raise InstallFailure("existing_runsc_runtime_path_conflicts")
    configured_args = runsc_config.get("runtimeArgs", [])
    if configured_args and configured_args not in (
        RUNTIME_ARGUMENTS,
        PREVIOUS_HOST_NETWORK_ARGUMENTS,
    ):
        raise InstallFailure("existing_runsc_runtime_arguments_conflict")
    runsc_config["path"] = runsc_path
    runsc_config["runtimeArgs"] = RUNTIME_ARGUMENTS
    return merged_config


def merge_runtime_configuration() -> None:
    """Validate and hot-reload the merged Docker runtime configuration."""
    runsc_path = shutil.which("runsc")
    if runsc_path is None:
        raise InstallFailure("runsc_binary_not_installed")
    runsc_path = str(Path(runsc_path).resolve())
    previous_contents = (
        DOCKER_CONFIG_PATH.read_bytes() if DOCKER_CONFIG_PATH.exists() else None
    )
    existing_mode = (
        DOCKER_CONFIG_PATH.stat().st_mode & 0o777
        if DOCKER_CONFIG_PATH.exists()
        else 0o600
    )
    try:
        docker_config = (
            json.loads(previous_contents.decode("utf-8")) if previous_contents else {}
        )
        if not isinstance(docker_config, dict):
            raise InstallFailure("docker_daemon_config_must_be_object")
        merged_config = merge_runtime_policy(docker_config, runsc_path)
        candidate_contents = (json.dumps(merged_config, indent=2) + "\n").encode(
            "utf-8"
        )
        temporary_path = DOCKER_CONFIG_PATH.with_name(
            f".{DOCKER_CONFIG_PATH.name}.validate-{os.getpid()}"
        )
        write_atomic(temporary_path, candidate_contents, mode=0o600)
        validation = subprocess.run(
            ["dockerd", "--validate", f"--config-file={temporary_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        temporary_path.unlink(missing_ok=True)
        if validation.returncode != 0:
            raise InstallFailure("merged_docker_daemon_config_failed_validation")
        if previous_contents != candidate_contents:
            backup_file(DOCKER_CONFIG_PATH)
            write_atomic(
                DOCKER_CONFIG_PATH,
                candidate_contents,
                mode=min(existing_mode or 0o600, 0o600),
            )
            run_capture(["systemctl", "reload", "docker"])
    except Exception:
        current_contents = (
            DOCKER_CONFIG_PATH.read_bytes() if DOCKER_CONFIG_PATH.exists() else None
        )
        if current_contents != previous_contents:
            if previous_contents is None:
                DOCKER_CONFIG_PATH.unlink(missing_ok=True)
            else:
                write_atomic(DOCKER_CONFIG_PATH, previous_contents, mode=existing_mode)
            subprocess.run(
                ["systemctl", "reload", "docker"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        raise


def ensure_runtime_live(docker_baseline: dict) -> None:
    """Confirm Docker loaded runsc without restarting the daemon or existing apps."""
    docker_info = json.loads(run_capture([*DOCKER, "info", "--format", "{{json .}}"]))
    current_state = read_docker_runtime_state()
    if "runsc" not in docker_info.get("Runtimes", {}):
        raise InstallFailure("docker_did_not_register_runsc")
    if current_state != docker_baseline:
        raise InstallFailure(
            "docker_or_existing_container_state_changed_during_runtime_install"
        )


def ensure_sandbox_network() -> None:
    """Create the private internal bridge or verify an identical existing one."""
    network_names = run_capture(
        [
            *DOCKER,
            "network",
            "ls",
            "--filter",
            f"name=^{NETWORK_NAME}$",
            "--format",
            "{{.Name}}",
        ]
    )
    if network_names:
        network = json.loads(
            run_capture(
                [*DOCKER, "network", "inspect", NETWORK_NAME, "--format", "{{json .}}"]
            )
        )
    else:
        run_visible(
            [
                *DOCKER,
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                NETWORK_NAME,
            ],
            timeout_seconds=60,
        )
        network = json.loads(
            run_capture(
                [*DOCKER, "network", "inspect", NETWORK_NAME, "--format", "{{json .}}"]
            )
        )
    if network.get("Driver") != "bridge" or network.get("Internal") is not True:
        raise InstallFailure("sandbox_network_policy_is_not_internal_bridge")
