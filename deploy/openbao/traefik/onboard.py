"""Onboard a FreshAI developer to the OpenBao API and its IP allowlist."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from onboard_account import UserpassAccount
from onboard_ingress import INSTALL_ROUTE, ROUTE_PATH, IngressChange, validate_public_ip

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INSTANCE_ROOT = REPOSITORY_ROOT / "config/private/openbao"


def _remote(ssh: list[str], command: list[str], *, content: bytes = b"") -> bytes:
    finished = subprocess.run(
        [*ssh, shlex.join(command)],
        input=content,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if finished.returncode == 3:
        raise RuntimeError(
            "The live route changed concurrently. Retry with the same arguments."
        )
    if finished.returncode != 0:
        raise RuntimeError(
            f"Remote {command[0]} failed (SSH exit {finished.returncode})."
        )
    return finished.stdout


def main() -> int:
    """Preview or apply the account and ingress changes.

    Returns:
        Zero on a successful preview or apply, one on an operational failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "instance", help="private instance directory name and SSH alias"
    )
    parser.add_argument("username", help="new personal OpenBao username")
    parser.add_argument("ip", help="colleague's public IPv4 or IPv6 address")
    parser.add_argument(
        "--apply", action="store_true", help="create the account and update ingress"
    )
    arguments = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", arguments.instance):
        parser.error(
            "Instance names may contain lowercase letters, digits and hyphens."
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", arguments.username):
        parser.error(
            "Usernames may contain lowercase letters, digits, underscores and hyphens."
        )
    try:
        network = validate_public_ip(arguments.ip)
    except ValueError as error:
        parser.error(str(error))

    instance_dir = INSTANCE_ROOT / arguments.instance
    ingress_path = instance_dir / "ingress.env"
    ssh_config = instance_dir / "ssh.conf"
    if not ingress_path.is_file() or not ssh_config.is_file():
        parser.error("The instance must have private ingress.env and ssh.conf files.")
    identity = Path(
        os.environ.get("ZATA_BACKUP_IDENTITY") or "~/.ssh/id_ed25519"
    ).expanduser()
    account = UserpassAccount(instance_dir, arguments.username, identity)
    ssh_base = [
        "ssh",
        "-F",
        str(ssh_config),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
    ]
    try:
        with ExitStack() as resources:
            if arguments.apply:
                lock = resources.enter_context(
                    (instance_dir / ".onboard.lock").open("a", encoding="utf-8")
                )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(
                        "Another onboarding run is active for this instance."
                    ) from None
            if not arguments.apply:
                live_route = _remote(
                    [*ssh_base, arguments.instance], ["cat", ROUTE_PATH]
                )
                change = IngressChange.plan(ingress_path, live_route, network)
                print(
                    f"Preview for {arguments.instance}: {arguments.username} with {network}"
                )
                print(
                    "Account will be created or verified using the encrypted local archive."
                )
                print(
                    f"Live allowlist: {'append' if change.route_update else 'already present'}"
                )
                print(
                    f"Local inventory: {'append' if change.inventory_update else 'already present'}"
                )
                return 0

            scratch_dir = Path(
                resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="bao-", dir="/tmp")
                )
            )
            control_path = scratch_dir / "ssh"
            with socket.socket() as reserved_socket:
                reserved_socket.bind(("127.0.0.1", 0))
                local_port = reserved_socket.getsockname()[1]
            ssh_control = [*ssh_base, "-S", str(control_path)]
            subprocess.run(
                [
                    *ssh_control,
                    "-M",
                    "-f",
                    "-N",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-L",
                    f"127.0.0.1:{local_port}:127.0.0.1:8200",
                    arguments.instance,
                ],
                check=True,
                timeout=25,
            )
            resources.callback(
                subprocess.run,
                [*ssh_control, "-O", "exit", arguments.instance],
                capture_output=True,
                check=False,
                timeout=15,
            )
            ssh = [*ssh_control, arguments.instance]
            live_route = _remote(ssh, ["cat", ROUTE_PATH])
            change = IngressChange.plan(ingress_path, live_route, network)
            if change.inventory_update or not account.archive.exists():
                subprocess.run(
                    [
                        sys.executable,
                        "scripts/diagnostics/private_config_backup.py",
                        "backup",
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=True,
                    timeout=120,
                )
            account.provision(local_port, scratch_dir)
            print(
                f"Verified {arguments.username}: development read allowed, test and production denied.",
                flush=True,
            )
            if change.route_update is not None:
                _remote(
                    ssh,
                    ["python3", "-c", INSTALL_ROUTE, ROUTE_PATH, change.live_digest],
                    content=change.route_update,
                )
            verified_route = _remote(ssh, ["cat", ROUTE_PATH])
            verified_change = IngressChange.plan(ingress_path, verified_route, network)
            if verified_change.route_update is not None:
                raise RuntimeError("The live route is missing the requested address.")
            verified_change.sync_inventory(ingress_path)
            print(f"OpenBao route and private inventory contain {network}.")
            print(
                f"Encrypted initial password: {account.archive.relative_to(REPOSITORY_ROOT)}"
            )
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        # Subprocess errors can contain arbitrary arguments. Surface only the command name.
        detail = str(error)
        if isinstance(error, subprocess.SubprocessError):
            detail = f"{Path(error.cmd[0]).name} failed or timed out"
        print(f"OpenBao onboarding failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
