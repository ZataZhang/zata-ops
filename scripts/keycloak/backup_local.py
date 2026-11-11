"""把线上 Keycloak 数据库备份到本机私有目录：开隧道、备份、关隧道。

数据库只绑定在服务器回环地址，因此经 SSH 隧道转发到本机后由 ``zata-ops db backup``
以 ``--local-dir`` 写入 ``config/private/backups/keycloak/``；服务器上不安装任何东西。
连接串来自私有凭据 ``KEYCLOAK_DATABASE_URL``（指向本机隧道端口），只注入子进程。
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from zata_ops.inventory import ServerConfig, load_inventory

REPO_ROOT = Path(__file__).resolve().parents[2]
TUNNEL_NAME = "keycloak-db"
LOCAL_PORT = 15432
REMOTE_PORT = 15432
SECRET_NAME = "KEYCLOAK_DATABASE_URL"


def _find_server(inventory_path: Path, server_name: str) -> ServerConfig:
    """从私有服务器清单中按名称找到服务器。

    Args:
        inventory_path: ``servers.toml`` 路径。
        server_name: 服务器名称。

    Returns:
        匹配的服务器配置。

    Raises:
        SystemExit: 服务器不存在或缺少 SSH 连接信息。
    """
    inventory = load_inventory(inventory_path)
    server = next(
        (item for item in inventory.servers if item.name == server_name), None
    )
    if server is None:
        names = ", ".join(item.name for item in inventory.servers)
        raise SystemExit(
            f"server {server_name!r} not found in {inventory_path}; known: {names}"
        )
    if not (server.public_ip or server.private_ip) or not server.ssh.user:
        raise SystemExit(
            f"server {server_name!r} has no SSH address or user in the inventory"
        )
    return server


def _tunnel_argv(server: ServerConfig) -> list[str]:
    """构造本机到服务器数据库回环端口的 SSH 转发命令。

    Args:
        server: 目标服务器配置。

    Returns:
        ``ssh`` argv。
    """
    ssh_argv = [
        "ssh",
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-p",
        str(server.ssh.port or 22),
        "-l",
        server.ssh.user,
        "-L",
        f"{LOCAL_PORT}:127.0.0.1:{REMOTE_PORT}",
    ]
    if server.ssh.identity_file:
        ssh_argv += ["-i", str(Path(server.ssh.identity_file).expanduser())]
    return ssh_argv + [str(server.public_ip or server.private_ip)]


def _wait_for_port(port: int, timeout_seconds: float = 15.0) -> bool:
    """等待本机端口可连接。

    Args:
        port: 本机端口。
        timeout_seconds: 最长等待秒数。

    Returns:
        端口在时限内可连接时为 ``True``。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _pg_dump_path_prefix() -> str:
    """返回包含 ``pg_dump`` 的目录，优先使用 Homebrew 的 libpq。

    Returns:
        需要前置到 ``PATH`` 的目录；系统 PATH 已有 ``pg_dump`` 时为空字符串。

    Raises:
        SystemExit: 本机找不到 ``pg_dump``。
    """
    if shutil.which("brew"):
        brew_prefix = subprocess.run(
            ["brew", "--prefix", "libpq"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if brew_prefix and (Path(brew_prefix) / "bin" / "pg_dump").exists():
            return str(Path(brew_prefix) / "bin")
    if shutil.which("pg_dump"):
        return ""
    raise SystemExit(
        "pg_dump not found; install the client tools first: brew install libpq"
    )


def main() -> int:
    """解析参数并执行一次本机备份。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", help="servers.toml 中运行 Keycloak 的服务器名")
    parser.add_argument("--inventory", default="config/private/servers.toml", type=Path)
    parser.add_argument("--local-dir", default="config/private/backups")
    parser.add_argument("--retention-days", default=30, type=int)
    options = parser.parse_args()

    server = _find_server(REPO_ROOT / options.inventory, options.server)
    path_prefix = _pg_dump_path_prefix()
    if _wait_for_port(LOCAL_PORT, timeout_seconds=0.5):
        raise SystemExit(
            f"local port {LOCAL_PORT} is already in use; close the other tunnel first"
        )

    subprocess.run(
        [
            "uv",
            "run",
            "zata-ops",
            "tunnel",
            "open",
            TUNNEL_NAME,
            "--",
            *_tunnel_argv(server),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        if not _wait_for_port(LOCAL_PORT):
            print("tunnel did not come up; see the tunnel log above", file=sys.stderr)
            return 1
        backup_command = (
            f'DATABASE_URL="${SECRET_NAME}" exec uv run zata-ops db backup --project keycloak '
            f"--s3-prefix keycloak --local-dir {options.local_dir} "
            f"--work-dir {options.local_dir}/.work --logs-dir /nonexistent "
            f"--resources-dir /nonexistent --retention-days {options.retention_days} --force-full"
        )
        backup_env_path = f"{path_prefix}:" if path_prefix else ""
        backup_returncode = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "scripts/diagnostics/private_secrets.py",
                "run",
                "--env",
                SECRET_NAME,
                "--show-output",
                "--",
                "sh",
                "-c",
                f'PATH="{backup_env_path}$PATH" {backup_command}',
            ],
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        # 备份含全部账号数据，只允许当前用户读取。
        backup_tree = REPO_ROOT / options.local_dir / "keycloak"
        for path in (
            [backup_tree, *backup_tree.rglob("*")] if backup_tree.exists() else []
        ):
            path.chmod(0o700 if path.is_dir() else 0o600)
        return backup_returncode
    finally:
        subprocess.run(
            ["uv", "run", "zata-ops", "tunnel", "close", TUNNEL_NAME],
            cwd=REPO_ROOT,
            check=False,
        )
        shutil.rmtree(REPO_ROOT / options.local_dir / ".work", ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
