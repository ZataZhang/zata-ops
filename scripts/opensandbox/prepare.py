"""Offline validation and opt-in host-local read-only preflight; never deploys."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path

from configuration import DATA_ROOT, NETWORK_NAME, load_settings


def _read_json_command(command: list[str]) -> dict:
    if command[0] == "docker":
        command = ["docker", "--host", "unix:///var/run/docker.sock", *command[1:]]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    return json.loads(completed.stdout)


def inspect_host() -> list[dict]:
    """Inspect the machine executing this script without pulling or starting images.

    Returns:
        Named pass/fail observations. A pass is a prerequisite, not a live test.
    """
    observations = []
    observations.append({"name": "linux", "passed": platform.system() == "Linux"})
    try:
        daemon_info = _read_json_command(["docker", "info", "--format", "{{json .}}"])
        observations.append(
            {
                "name": "docker_runsc_registered",
                "passed": "runsc" in daemon_info.get("Runtimes", {}),
            }
        )
        docker_root = Path(daemon_info["DockerRootDir"])
        observations.append(
            {
                "name": "docker_disk_10gib",
                "passed": shutil.disk_usage(docker_root).free >= 10 * 1024**3,
            }
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        observations.append({"name": "docker_readable", "passed": False})
    try:
        daemon_config = json.loads(
            Path("/etc/docker/daemon.json").read_text(encoding="utf-8")
        )
        runtime_args = (
            daemon_config.get("runtimes", {}).get("runsc", {}).get("runtimeArgs", [])
        )
        observations.append(
            {"name": "runsc_systrap", "passed": "--platform=systrap" in runtime_args}
        )
        observations.append(
            {"name": "runsc_network", "passed": "--network=host" in runtime_args}
        )
        observations.append(
            {
                "name": "runsc_docker_visible_files",
                "passed": "--overlay2=none" in runtime_args,
            }
        )
    except (OSError, ValueError, AttributeError):
        observations.append({"name": "daemon_configuration_readable", "passed": False})
    try:
        network_info = _read_json_command(
            ["docker", "network", "inspect", NETWORK_NAME, "--format", "{{json .}}"]
        )
        observations.append(
            {
                "name": "internal_network",
                "passed": network_info.get("Internal") is True
                and network_info.get("Driver") == "bridge",
            }
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        observations.append({"name": "network_and_context_readable", "passed": False})
    try:
        mounted = subprocess.run(
            ["findmnt", "--mountpoint", "/mnt/data", "--noheadings"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        observations.append({"name": "data_mount", "passed": mounted.returncode == 0})
        observations.append(
            {
                "name": "data_disk_20gib",
                "passed": shutil.disk_usage(DATA_ROOT).free >= 20 * 1024**3,
            }
        )
        observations.append(
            {
                "name": "state_directories",
                "passed": all(
                    (DATA_ROOT / suffix).is_dir()
                    for suffix in ["control-plane", "workspaces"]
                ),
            }
        )
        memory_fields = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        )
        available_kib = int(memory_fields["MemAvailable"].split()[0])
        observations.append(
            {"name": "memory_4gib_available", "passed": available_kib >= 4 * 1024**2}
        )
        ports = subprocess.run(
            ["ss", "-H", "-ltn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        occupied = []
        for socket_line in ports.stdout.splitlines():
            socket_columns = socket_line.split()
            local_port = int(socket_columns[3].rsplit(":", 1)[1])
            if local_port == 18080 or 41000 <= local_port <= 41100:
                occupied.append(local_port)
        observations.append(
            {
                "name": "reserved_ports_free",
                "passed": not occupied,
                "occupied_count": len(occupied),
            }
        )
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        observations.append({"name": "host_resources_readable", "passed": False})
    return observations


def main() -> int:
    """Validate files or inspect only the local host when explicitly requested.

    Returns:
        Zero on success, one on a failed host preflight, two on invalid files.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "preflight"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--example",
        action="store_true",
        help="Offline sample validation only; never readiness evidence",
    )
    arguments = parser.parse_args()
    try:
        load_settings(arguments.config, arguments.env_file, example=arguments.example)
        if arguments.example and arguments.action != "validate":
            raise ValueError("examples_are_offline_only")
    except (OSError, ValueError):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "invalid_private_configuration",
                    "network_access": False,
                }
            )
        )
        return 2
    if arguments.action == "validate":
        print(
            json.dumps(
                {
                    "status": "example_valid"
                    if arguments.example
                    else "configuration_valid",
                    "network_access": False,
                    "deployment_ready": False,
                }
            )
        )
        return 0
    observations = inspect_host()
    passed = all(observation["passed"] for observation in observations)
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "checks": observations,
                "mutations": False,
                "live_sandbox_verified": False,
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
