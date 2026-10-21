"""Install the pinned OpenSandbox pilot on a prepared Ubuntu 24.04 Docker host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from configuration import DATA_ROOT, NETWORK_NAME, load_settings, normalize_data_root
from runtime import (
    DOCKER,
    DOCKER_CONFIG_PATH,
    SYSTEMD_UNIT_PATH,
    InstallFailure,
    RUNTIME_ARGUMENTS,
    backup_file,
    ensure_gvisor_repository,
    ensure_runtime_live,
    ensure_sandbox_network,
    merge_runtime_configuration,
    read_docker_runtime_state,
    run_capture,
    run_visible,
    write_atomic,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = REPOSITORY_ROOT / "deploy/opensandbox"
TOOL_SOURCE_ROOT = REPOSITORY_ROOT / "scripts/opensandbox"
RESERVED_PORTS = {18080, *range(41000, 41101)}
INSTALL_MARKER = Path("/opt/opensandbox/.zata-ops-managed")
INSTALL_MARKER_CONTENT = "managed-by-zata-ops-opensandbox-v1\n"


def inspect_prerequisites(
    data_root: Path,
    require_mountpoint: Path | None = None,
    *,
    allow_partial: bool = False,
) -> dict:
    """Verify platform capacity and existing services before installation writes."""
    if os.geteuid() != 0:
        raise InstallFailure("run_with_root_privileges")
    release_text = Path("/etc/os-release").read_text(encoding="utf-8")
    if not re.search(r"^ID=\"?ubuntu\"?$", release_text, re.MULTILINE):
        raise InstallFailure("supported_platform_is_ubuntu_24_04")
    if not re.search(r"^VERSION_ID=\"?24\.04\"?$", release_text, re.MULTILINE):
        raise InstallFailure("supported_platform_is_ubuntu_24_04")
    if platform.machine() != "x86_64":
        raise InstallFailure("requirements_lock_supports_x86_64_only")
    if shutil.which("python3.12") is None or shutil.which("apt-get") is None:
        raise InstallFailure("python_3_12_and_apt_required")
    if shutil.which("docker") is None:
        raise InstallFailure("docker_cli_required")
    partial_state_paths = (
        Path("/opt/opensandbox"),
        SYSTEMD_UNIT_PATH,
        Path("/etc/opensandbox"),
    )
    partial_state_exists = any(
        path.exists() or path.is_symlink() for path in partial_state_paths
    )
    if partial_state_exists:
        if not allow_partial:
            raise InstallFailure("installation_state_already_exists_use_resume_partial")
        if INSTALL_MARKER.is_symlink() or not INSTALL_MARKER.is_file():
            raise InstallFailure("partial_install_marker_missing_or_invalid")
        if INSTALL_MARKER.read_text(encoding="utf-8") != INSTALL_MARKER_CONTENT:
            raise InstallFailure("partial_install_marker_unrecognized")
        for state_path in partial_state_paths:
            if state_path.is_symlink():
                raise InstallFailure("partial_install_path_must_not_be_symlink")
    data_root = normalize_data_root(data_root)
    if require_mountpoint is not None:
        mountpoint = normalize_data_root(require_mountpoint / "opensandbox").parent
        if not data_root.is_relative_to(mountpoint):
            raise InstallFailure("data_root_must_be_below_required_mountpoint")
        mount_status = subprocess.run(
            ["findmnt", "--mountpoint", str(mountpoint), "--noheadings"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if mount_status.returncode != 0:
            raise InstallFailure("required_data_mountpoint_is_not_mounted")
    if data_root.exists():
        if not data_root.is_dir() or data_root.is_symlink():
            raise InstallFailure("data_root_must_be_directory")
        unexpected_entries = {
            child.name
            for child in data_root.iterdir()
            if child.name not in {"control-plane", "workspaces"}
        }
        if unexpected_entries:
            raise InstallFailure("data_root_contains_unmanaged_entries")
        if any(
            (data_root / child).is_symlink()
            for child in ("control-plane", "workspaces")
        ):
            raise InstallFailure("data_root_child_must_not_be_symlink")
    docker_info = json.loads(run_capture([*DOCKER, "info", "--format", "{{json .}}"]))
    if docker_info.get("ServerVersion") is None:
        raise InstallFailure("docker_daemon_unavailable")
    docker_root = Path(docker_info["DockerRootDir"])
    existing_data_ancestor = data_root
    while not existing_data_ancestor.exists() and existing_data_ancestor != Path("/"):
        existing_data_ancestor = existing_data_ancestor.parent
    if shutil.disk_usage(docker_root).free < 10 * 1024**3:
        raise InstallFailure("docker_storage_requires_10_gib_free")
    if shutil.disk_usage(existing_data_ancestor).free < 20 * 1024**3:
        raise InstallFailure("data_storage_requires_20_gib_free")
    memory_fields = dict(
        line.split(":", 1)
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    )
    available_kib = int(memory_fields["MemAvailable"].split()[0])
    if available_kib < 4 * 1024**2:
        raise InstallFailure("at_least_4_gib_available_memory_required")
    if run_capture(["systemctl", "is-active", "docker"]) != "active":
        raise InstallFailure("docker_service_must_be_active")
    if (
        run_capture(["systemctl", "show", "docker", "-p", "CanReload"])
        != "CanReload=yes"
    ):
        raise InstallFailure("docker_systemd_reload_required_for_no_restart_install")
    listening_ports = run_capture(["ss", "-H", "-lnt"])
    occupied_ports = []
    for socket_line in listening_ports.splitlines():
        socket_columns = socket_line.split()
        try:
            local_port = int(socket_columns[3].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if local_port in RESERVED_PORTS:
            occupied_ports.append(local_port)
    if occupied_ports:
        raise InstallFailure("sandbox_reserved_ports_are_occupied")
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
    network_json = (
        run_capture(
            [*DOCKER, "network", "inspect", NETWORK_NAME, "--format", "{{json .}}"]
        )
        if network_names
        else ""
    )
    if network_json:
        network = json.loads(network_json)
        if network.get("Driver") != "bridge" or network.get("Internal") is not True:
            raise InstallFailure("existing_sandbox_network_has_conflicting_policy")
    if DOCKER_CONFIG_PATH.exists():
        if DOCKER_CONFIG_PATH.is_symlink():
            raise InstallFailure("docker_daemon_config_symlink_not_supported")
        try:
            docker_config = json.loads(DOCKER_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as config_error:
            raise InstallFailure(
                "docker_daemon_config_is_invalid_json"
            ) from config_error
        if not isinstance(docker_config, dict):
            raise InstallFailure("docker_daemon_config_must_be_object")
    return {
        "data_root": str(data_root),
        "docker_root": str(docker_root),
        "docker_server_version": docker_info["ServerVersion"],
        "docker_runtime_default": docker_info.get("DefaultRuntime"),
        "memory_available_gib": round(available_kib / 1024**2, 2),
        "ports_free": True,
        "network_ready": bool(network_json),
    }


def install_python_environments() -> None:
    """Install the hash-locked server and acceptance SDK into separate venvs."""
    installation_root = Path("/opt/opensandbox")
    server_venv = installation_root / "venv"
    smoke_venv = installation_root / "smoke-venv"
    run_visible(["python3.12", "-m", "venv", str(server_venv)], timeout_seconds=120)
    run_visible(
        [
            str(server_venv / "bin/pip"),
            "install",
            "--require-hashes",
            "--disable-pip-version-check",
            "-r",
            str(PROFILE_ROOT / "requirements.txt"),
        ],
        timeout_seconds=900,
    )
    run_visible(["python3.12", "-m", "venv", str(smoke_venv)], timeout_seconds=120)
    run_visible(
        [
            str(smoke_venv / "bin/pip"),
            "install",
            "--require-hashes",
            "--disable-pip-version-check",
            "-r",
            str(PROFILE_ROOT / "requirements-smoke.txt"),
        ],
        timeout_seconds=600,
    )


def install_files(settings, data_root: Path) -> None:
    """Install private config, validators, persistent directories and systemd unit."""
    installation_root = Path("/opt/opensandbox")
    tool_directory = installation_root / "tools"
    tool_directory.mkdir(mode=0o755, exist_ok=True)
    for tool_name in ("configuration.py", "prepare.py", "smoke.py"):
        shutil.copyfile(TOOL_SOURCE_ROOT / tool_name, tool_directory / tool_name)
        (tool_directory / tool_name).chmod(0o644)
    data_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    data_root.chmod(0o700)
    for directory_name in ("control-plane", "workspaces"):
        data_directory = data_root / directory_name
        data_directory.mkdir(mode=0o700, exist_ok=True)
        data_directory.chmod(0o700)
    private_config_directory = Path("/etc/opensandbox")
    private_config_directory.mkdir(mode=0o700, exist_ok=True)
    private_config_directory.chmod(0o700)
    for source_path, target_path in (
        (settings.config_path, private_config_directory / "config.toml"),
        (settings.env_path, private_config_directory / "server.env"),
    ):
        if target_path.exists():
            if target_path.read_bytes() != source_path.read_bytes():
                raise InstallFailure("existing_private_configuration_conflicts")
        else:
            shutil.copyfile(source_path, target_path)
        target_path.chmod(0o600)
    preflight = subprocess.run(
        [
            str(installation_root / "venv/bin/python"),
            str(tool_directory / "prepare.py"),
            "preflight",
            "--config",
            str(private_config_directory / "config.toml"),
            "--env-file",
            str(private_config_directory / "server.env"),
            "--data-root",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if preflight.returncode != 0:
        raise InstallFailure(
            "host_preflight_failed_after_setup:" + preflight.stdout.strip()
        )
    unit_template = (PROFILE_ROOT / "opensandbox.service").read_text(encoding="utf-8")
    rendered_unit = unit_template.replace("@DATA_ROOT@", str(data_root))
    if "@DATA_ROOT@" in rendered_unit:
        raise InstallFailure("systemd_data_root_template_not_rendered")
    if SYSTEMD_UNIT_PATH.exists():
        if SYSTEMD_UNIT_PATH.read_text(encoding="utf-8") != rendered_unit:
            raise InstallFailure("existing_systemd_unit_conflicts")
    else:
        write_atomic(SYSTEMD_UNIT_PATH, rendered_unit.encode("utf-8"), mode=0o644)


def start_service() -> None:
    """Enable the server and confirm its private API listener becomes active."""
    run_visible(["systemctl", "daemon-reload"], timeout_seconds=60)
    run_visible(
        ["systemctl", "enable", "--now", "opensandbox.service"], timeout_seconds=120
    )
    for _ in range(60):
        service_status = subprocess.run(
            ["systemctl", "is-active", "opensandbox.service"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        socket_status = subprocess.run(
            ["ss", "-H", "-lnt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        private_listener = any(
            line.split()[3].startswith("127.0.0.1:")
            and line.split()[3].endswith(":18080")
            for line in socket_status.stdout.splitlines()
            if len(line.split()) >= 4
        )
        if service_status.stdout.strip() == "active" and private_listener:
            return
        time.sleep(1)
    raise InstallFailure("opensandbox_service_did_not_become_ready")


def write_deployment_record(
    data_root: Path, package_version: str, runsc_version: str
) -> None:
    """Record non-secret versions, paths and lockfile hashes under /etc/opensandbox."""
    configuration_path = Path("/etc/opensandbox/config.toml")
    installed_package_version = run_capture(
        ["dpkg-query", "-W", "-f=${Version}", "runsc"]
    )
    docker_info = json.loads(run_capture([*DOCKER, "info", "--format", "{{json .}}"]))
    deployment_record = {
        "installed_at_utc": datetime.now(UTC).isoformat(),
        "opensandbox_server": "1.1.1",
        "runsc_apt_candidate": package_version,
        "runsc_package_version": installed_package_version,
        "runsc_version": runsc_version,
        "docker_server_version": docker_info.get("ServerVersion"),
        "docker_default_runtime": docker_info.get("DefaultRuntime"),
        "runsc_runtime_args": RUNTIME_ARGUMENTS,
        "data_root": str(data_root),
        "configuration_sha256": hashlib.sha256(
            configuration_path.read_bytes()
        ).hexdigest(),
        "server_requirements_sha256": hashlib.sha256(
            (PROFILE_ROOT / "requirements.txt").read_bytes()
        ).hexdigest(),
        "smoke_requirements_sha256": hashlib.sha256(
            (PROFILE_ROOT / "requirements-smoke.txt").read_bytes()
        ).hexdigest(),
        "sandbox_network": NETWORK_NAME,
        "api_listener": "127.0.0.1:18080",
        "docker_daemon_restarted": False,
    }
    write_atomic(
        Path("/etc/opensandbox/deployment.json"),
        (json.dumps(deployment_record, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def install(arguments: argparse.Namespace) -> dict:
    """Perform a first installation after a clean target-specific preflight."""
    data_root = normalize_data_root(arguments.data_root)
    settings = load_settings(
        arguments.config,
        arguments.env_file,
        data_root=data_root,
    )
    target_report = inspect_prerequisites(
        data_root,
        arguments.require_mountpoint,
        allow_partial=arguments.resume_partial,
    )
    docker_baseline = read_docker_runtime_state()
    print("Host preflight passed; installing signed gVisor packages.", flush=True)
    Path("/opt/opensandbox").mkdir(mode=0o755, exist_ok=True)
    Path("/opt/opensandbox").chmod(0o755)
    if INSTALL_MARKER.exists():
        if INSTALL_MARKER.read_text(encoding="utf-8") != INSTALL_MARKER_CONTENT:
            raise InstallFailure("partial_install_marker_unrecognized")
    else:
        write_atomic(INSTALL_MARKER, INSTALL_MARKER_CONTENT.encode("utf-8"), mode=0o644)
    if DOCKER_CONFIG_PATH.exists():
        backup_file(DOCKER_CONFIG_PATH)
    selected_package_version = ensure_gvisor_repository()
    merge_runtime_configuration()
    ensure_runtime_live(docker_baseline)
    runsc_version = run_capture([shutil.which("runsc") or "runsc", "--version"])
    ensure_sandbox_network()
    install_python_environments()
    install_files(settings, data_root)
    start_service()
    write_deployment_record(data_root, selected_package_version, runsc_version)
    return {
        "status": "installed",
        "data_root": str(data_root),
        "server": "1.1.1",
        "runsc_package_version": selected_package_version,
        "runsc_version": runsc_version,
        "api_listener": "127.0.0.1:18080",
        "docker_daemon_restarted": False,
        "existing_container_count": len(docker_baseline["container_states"]),
        "preflight": target_report,
    }


def main() -> int:
    """Validate an installation plan or explicitly apply it on the target host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument(
        "--require-mountpoint",
        type=Path,
        help="Require a configured mount point such as /mnt/data before writing there",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Install packages, configure Docker and start service",
    )
    parser.add_argument(
        "--check-host",
        action="store_true",
        help="Run read-only host checks without installing anything",
    )
    parser.add_argument(
        "--resume-partial",
        action="store_true",
        help="Resume only a deployment directory marked by this installer",
    )
    arguments = parser.parse_args()
    try:
        normalized_data_root = normalize_data_root(arguments.data_root)
        load_settings(
            arguments.config, arguments.env_file, data_root=normalized_data_root
        )
        if not arguments.apply:
            if arguments.check_host:
                host_report = inspect_prerequisites(
                    normalized_data_root,
                    arguments.require_mountpoint,
                    allow_partial=arguments.resume_partial,
                )
                print(
                    json.dumps(
                        {
                            "status": "host_preflight_passed",
                            "mutations": False,
                            "network_access": False,
                            "checks": host_report,
                        },
                        indent=2,
                    )
                )
                return 0
            plan = {
                "status": "planned",
                "mutations": False,
                "network_access": False,
                "data_root": str(normalized_data_root),
                "required_mountpoint": (
                    str(arguments.require_mountpoint)
                    if arguments.require_mountpoint
                    else None
                ),
                "steps": [
                    "install signed gVisor runsc package",
                    "merge and hot-reload Docker runtime configuration",
                    "create internal sandbox bridge",
                    "install hash-locked OpenSandbox and SDK environments",
                    "install private configuration and systemd unit",
                    "start loopback-only API",
                ],
            }
            print(json.dumps(plan, indent=2))
            return 0
        result = install(arguments)
    except (
        InstallFailure,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as install_error:
        error_message = (
            str(install_error)
            if isinstance(install_error, InstallFailure)
            else "invalid_or_unavailable_installation_input"
        )
        print(
            json.dumps({"status": "failed", "reason": error_message}), file=sys.stderr
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
