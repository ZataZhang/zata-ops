# /// script
# requires-python = ">=3.11"
# dependencies = ["opensandbox==1.1.1"]
# ///
"""Opt-in, host-local SDK acceptance probe; offline plan is the default."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

from configuration import NETWORK_NAME, is_pinned_image, load_settings

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]


def _owned_containers(marker: str) -> list[str]:
    completed = subprocess.run(
        [
            *DOCKER,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=zata-ops-smoke={marker}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    return completed.stdout.split()


async def run_smoke(api_key: str, image: str) -> dict:
    """Create one bounded sandbox and verify SDK I/O, runtime and cleanup.

    Args:
        api_key: Private platform authentication key.
        image: Approved immutable workload image containing Python 3.

    Returns:
        Evidence report; unknown creation/cleanup outcomes remain failures.
    """
    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig
    from opensandbox.models import WriteEntry
    from opensandbox.models.execd import RunCommandOpts

    marker = uuid.uuid4().hex
    report = {
        "status": "failed",
        "correlation": marker,
        "checks": {},
        "cleanup": "unknown",
    }
    sandbox = None
    print(json.dumps({"status": "starting", "correlation": marker}), flush=True)
    try:
        connection = ConnectionConfig(
            domain="127.0.0.1:18080",
            protocol="http",
            api_key=api_key,
            use_server_proxy=True,
            request_timeout=timedelta(seconds=30),
        )
        sandbox = await Sandbox.create(
            image,
            connection_config=connection,
            timeout=timedelta(seconds=180),
            ready_timeout=timedelta(seconds=60),
            resource={"cpu": "500m", "memory": "512Mi"},
            entrypoint=["python3", "-c", "import time; time.sleep(180)"],
            metadata={"zata-ops-smoke": marker},
        )
        report["sandbox_id"] = sandbox.id
        container_ids = _owned_containers(marker)
        if len(container_ids) != 1:
            raise RuntimeError("sandbox_container_not_unique")
        inspected = subprocess.run(
            [*DOCKER, "inspect", container_ids[0]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        container = json.loads(inspected.stdout)[0]
        host_config = container["HostConfig"]
        report["checks"]["runtime_runsc"] = host_config["Runtime"] == "runsc"
        report["checks"]["container_identity"] = (
            container["Config"]["Labels"].get("opensandbox.io/id") == sandbox.id
        )
        report["checks"]["isolated_network"] = (
            host_config["NetworkMode"] == NETWORK_NAME
        )
        # Fail before command/file execution if the platform silently downgraded runtime.
        if not all(report["checks"].values()):
            raise RuntimeError("runtime_policy_mismatch")
        expected_text = f"zata-ops-smoke:{marker}\n"
        await sandbox.files.write_files(
            [WriteEntry(path="/tmp/zata-ops-smoke.txt", data=expected_text)]
        )
        read_text = await sandbox.files.read_file("/tmp/zata-ops-smoke.txt")
        report["checks"]["file_roundtrip"] = read_text == expected_text
        execution = await sandbox.commands.run(
            [
                "python3",
                "-c",
                "from pathlib import Path; print(Path('/tmp/zata-ops-smoke.txt').read_text(), end='')",
            ],
            opts=RunCommandOpts(timeout=timedelta(seconds=15)),
        )
        stdout_text = "".join(message.text for message in execution.logs.stdout)
        report["checks"]["command_roundtrip"] = (
            execution.exit_code == 0
            and execution.error is None
            and stdout_text == expected_text
        )
        if all(report["checks"].values()):
            report["status"] = "passed"
    except Exception as smoke_error:
        report["error_type"] = type(smoke_error).__name__
    finally:
        if sandbox is not None:
            try:
                await sandbox.kill()
            except Exception:
                report["cleanup"] = "kill_unconfirmed"
            finally:
                try:
                    await sandbox.close()
                except Exception:
                    report["client_close"] = "failed"
                    report["status"] = "failed"
        try:
            remaining = _owned_containers(marker)
            report["cleanup"] = "confirmed_absent" if not remaining else "pending"
            report["remaining_container_ids"] = remaining
        except (OSError, subprocess.SubprocessError):
            report["cleanup"] = "unverified"
        if sandbox is None or report["cleanup"] != "confirmed_absent":
            report["status"] = "failed"
    return report


def main() -> int:
    """Print an offline plan unless a caller explicitly requests sandbox creation.

    Returns:
        Zero for a plan or passing smoke, one for failed acceptance, two for bad inputs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-create", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--image", help="Approved Python image pinned by sha256 digest")
    arguments = parser.parse_args()
    if not arguments.allow_create:
        print(
            json.dumps(
                {
                    "status": "planned",
                    "network_access": False,
                    "creates_sandbox": False,
                    "checks": [
                        "sdk_command",
                        "sdk_file",
                        "actual_runsc",
                        "instance_identity",
                        "cleanup",
                    ],
                }
            )
        )
        return 0
    if not arguments.config or not arguments.env_file or not arguments.image:
        parser.error("--allow-create requires --config, --env-file and --image")
    if not is_pinned_image(arguments.image):
        parser.error("--image must use an immutable sha256 digest")
    try:
        settings = load_settings(arguments.config, arguments.env_file)
        report = asyncio.run(
            asyncio.wait_for(run_smoke(settings.api_key, arguments.image), timeout=150)
        )
    except Exception as smoke_error:
        # Never print SDK exception messages, request headers, or configuration values.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(smoke_error).__name__,
                    "cleanup": "unverified",
                }
            )
        )
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
