"""Register services interactively; show metadata or use linked credentials."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import ValidationError
from private_secrets import (
    credential_status,
    read_hidden_secret,
    run_with_credentials,
    validate_private_directory,
)

from zata_ops.inventory import load_inventory
from zata_ops.services import (
    ServiceConfig,
    ServicesConfig,
    load_services,
    parse_services_config as parse_services_config,
    summarize_services,
    write_services_config,
)
from zata_ops.private_credentials import store_secret


def add_service(private_dir: Path, inventory: ServicesConfig) -> None:
    """Collect metadata and a hidden password, then save their association.

    Args:
        private_dir: Ignored private configuration directory.
        inventory: Existing services, preserved when adding a new one.

    Returns:
        None.

    Raises:
        ValueError: Input is invalid or the service already exists.
        OSError: Backup or writing fails.
    """
    if not sys.stdin.isatty():
        raise ValueError("interactive terminal required")
    print("添加服务：地址和用户名可供 Agent 查询；密码隐藏保存。Ctrl+C 可取消。")
    name = input("服务名称（例如 main-postgres 或 主数据库）：").strip()
    if not name or any(service.name == name for service in inventory.services):
        print("服务名称为空或已存在；未修改配置。")
        raise ValueError("duplicate or empty service")
    service_type = (
        input("服务类型 postgresql/mysql/redis/http/other [postgresql]：").strip()
        or "postgresql"
    )
    server_names = []
    server_path = private_dir / "servers.toml"
    if server_path.exists():
        server_names = [server.name for server in load_inventory(server_path).servers]
    if server_names:
        print("已登记服务器：" + "、".join(server_names))
    server_name = input("所属服务器（填已登记名称；外部服务留空）：").strip()
    if server_name and server_name not in server_names:
        print("服务器尚未登记；请先登记服务器，外部服务请留空。")
        raise ValueError("unknown server")
    print(
        "URL 不含账号密码、查询参数或 Token；带凭据的完整连接串请用 secret-set 保存。"
    )
    public_url = ServiceConfig.validate_public_url(
        input("服务 URL（可留空）：").strip()
    )
    parsed_url = urlsplit(public_url)
    default_host = parsed_url.hostname or ""
    host = input(f"主机地址 [{default_host}]：").strip() or default_host
    default_port = parsed_url.port or {
        "postgresql": 5432,
        "mysql": 3306,
        "redis": 6379,
        "http": 443 if parsed_url.scheme == "https" else 80,
    }.get(service_type, "")
    port_text = input(f"端口 [{default_port}]：").strip() or str(default_port)
    database = input("数据库名（非数据库服务可留空）：").strip()
    username = input("用户名（无账号可留空）：").strip()
    notes = input("用途 / 备注（可留空）：").strip()
    scope = input("权限范围 scope（如 Viewer、只读；可留空）：").strip()
    today_text = date.today().isoformat()
    created_at = input(f"创建日期 YYYY-MM-DD [{today_text}]：").strip() or today_text
    expires_at = input("过期日期 YYYY-MM-DD（不过期留空）：").strip()
    existing_reference = input(
        "已用 just secret-set 保存的凭据名（如 GRAFANA_XXX_TOKEN；留空则稍后隐藏输入）："
    ).strip()
    credentials = dotenv_values(
        private_dir / "secrets.env", interpolate=False, encoding="utf-8"
    )
    if existing_reference:
        # 关联已有凭据：名称须已存在且未被其他服务占用，不重新输入密码。
        reference = existing_reference
        if reference not in credentials:
            print("该凭据名尚未保存；请先运行 just secret-set。未修改配置。")
            raise ValueError("unknown credential reference")
        if any(service.password_env == reference for service in inventory.services):
            print("该凭据已关联其他服务；未修改配置。")
            raise ValueError("credential reference already linked")
    else:
        reference = (
            "SERVICE_"
            + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16].upper()
            + "_PASSWORD"
        )
        if reference in credentials or any(
            service.password_env == reference for service in inventory.services
        ):
            raise ValueError("generated credential reference already exists")
    service = ServiceConfig(
        name=name,
        server=server_name,
        type=service_type,
        host=host,
        port=int(port_text) if port_text else "",
        database=database,
        username=username,
        url=public_url,
        password_env=reference,
        notes=notes,
        scope=scope,
        created_at=created_at,
        expires_at=expires_at,
    )
    if not service.host:
        print("需要填写主机地址或服务 URL；未修改配置。")
        raise ValueError("missing host")
    password = ""
    if not existing_reference:
        print("接下来输入该服务现有的密码或 Token，不会创建或更改远端账号。")
        password = read_hidden_secret("密码 / Token（隐藏输入，输入后回车）：")
        confirmation = read_hidden_secret("再次输入密码 / Token：")
        if password != confirmation:
            print("两次输入不一致；未保存。")
            raise ValueError("password mismatch")
    if input("保存此服务及凭据关联？[y/N]：").strip().lower() != "y":
        print("已取消，未保存。")
        return
    updated_inventory = ServicesConfig(services=[*inventory.services, service])
    if not existing_reference:
        store_secret(private_dir / "secrets.env", reference, password)
    write_services_config(private_dir / "services.toml", updated_inventory)
    print("服务已保存，凭据已关联且未显示。运行 just service-list 查看。")


def warn_expiring_credentials(service_summaries: list[dict[str, object]]) -> None:
    """Print expiry warnings to stderr so JSON on stdout stays parseable.

    Args:
        service_summaries: Output of ``summarize_services``.

    Returns:
        None.
    """
    for summary in service_summaries:
        if summary["expiry_status"] == "expired":
            print(
                f"⚠ {summary['name']} 的凭据已于 {summary['expires_at']} 过期，需要轮换。",
                file=sys.stderr,
            )
        elif summary["expiry_status"] == "expiring":
            print(
                f"⚠ {summary['name']} 的凭据将在 {summary['days_until_expiry']} 天后"
                f"（{summary['expires_at']}）过期。",
                file=sys.stderr,
            )


def main() -> int:
    """Run service registration, safe metadata queries or credential injection.

    Returns:
        Zero on success, one on input/storage errors, or the child exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, default=Path("config/private"))
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("add")
    commands.add_parser("list")
    show_parser = commands.add_parser("show")
    show_parser.add_argument("name")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("name")
    run_parser.add_argument("--password-env", default="SERVICE_PASSWORD")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    try:
        validate_private_directory(arguments.private_dir)
        inventory = load_services(arguments.private_dir / "services.toml")
        if arguments.action == "add":
            add_service(arguments.private_dir, inventory)
            return 0
        selected = (
            inventory.services
            if arguments.action == "list"
            else [
                service
                for service in inventory.services
                if service.name == arguments.name
            ]
        )
        if arguments.action != "list" and not selected:
            print("未找到该服务。", file=sys.stderr)
            return 1
        if arguments.action == "run":
            command = (
                arguments.command[1:]
                if arguments.command[:1] == ["--"]
                else arguments.command
            )
            return run_with_credentials(
                arguments.private_dir / "secrets.env",
                {arguments.password_env: selected[0].password_env},
                command,
            )
        configured_credentials = credential_status(
            arguments.private_dir / "secrets.env"
        )
        summaries = summarize_services(selected, configured_credentials)
        if not summaries:
            print("尚未登记服务。运行 just service-add 添加。")
        else:
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
            warn_expiring_credentials(summaries)
        return 0
    except (OSError, ValueError, ValidationError, getpass.GetPassWarning):
        print(
            "服务操作失败：请检查输入、URL 格式、配置和文件权限；密码未显示。",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消；如尚未确认保存，配置未修改。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
