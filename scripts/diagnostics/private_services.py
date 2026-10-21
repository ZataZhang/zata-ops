"""Register services interactively; show metadata or use linked credentials."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, ValidationError, field_validator, model_validator
from private_secrets import (
    SECRET_NAME,
    read_hidden_secret,
    run_with_credentials,
    store_secret,
    validate_private_directory,
)

from zata_ops.inventory import InventoryRecord, load_inventory


class ServiceConfig(InventoryRecord):
    """Public connection metadata with a reference to a separate credential."""

    name: Annotated[str, Field(min_length=1)]
    server: str
    type: Literal["postgresql", "mysql", "redis", "http", "other"]
    host: str
    port: Annotated[int, Field(ge=1, le=65535)] | Literal[""]
    database: str
    username: str
    url: str = ""
    password_env: str
    notes: str

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        """Reject URL fields that could contain credentials.

        Args:
            value: Public service URL, or an empty string.

        Returns:
            Validated URL.

        Raises:
            ValueError: URL has credentials, query parameters or a fragment.
        """
        if value:
            parsed = urlsplit(value)
            if (
                not parsed.scheme
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("URL must have no credentials, query or fragment")
            parsed.port
        return value

    @field_validator("password_env")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        """Check that a credential reference is an environment variable name.

        Args:
            value: Reference to the private credential store.

        Returns:
            Validated reference.

        Raises:
            ValueError: Reference is invalid.
        """
        if not SECRET_NAME.fullmatch(value):
            raise ValueError("invalid credential reference")
        return value


class ServicesConfig(InventoryRecord):
    """Versioned service list with unique names and credential references."""

    schema_version: Literal[1] = 1
    services: list[ServiceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_services(self) -> ServicesConfig:
        """Require unique service names and references.

        Returns:
            Validated service list.

        Raises:
            ValueError: Names or credential references are duplicated.
        """
        for values in (
            [service.name for service in self.services],
            [service.password_env for service in self.services],
        ):
            if len(values) != len(set(values)):
                raise ValueError("duplicate services or credential references")
        return self


def load_services(service_path: Path) -> ServicesConfig:
    """Load and validate service metadata without exposing invalid inputs.

    Args:
        service_path: Private service TOML file.

    Returns:
        Validated inventory; empty inventory when no file exists.

    Raises:
        ValueError: TOML or fields are invalid, or the file is a symlink.
        OSError: File cannot be read.
    """
    if service_path.is_symlink():
        raise ValueError("symlink service file")
    if not service_path.exists():
        return ServicesConfig()
    service_path.chmod(0o600)
    return ServicesConfig.model_validate_json(
        json.dumps(tomllib.loads(service_path.read_text(encoding="utf-8")))
    )


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
    reference = (
        "SERVICE_"
        + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16].upper()
        + "_PASSWORD"
    )
    credentials = dotenv_values(
        private_dir / "secrets.env", interpolate=False, encoding="utf-8"
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
    )
    if not service.host:
        print("需要填写主机地址或服务 URL；未修改配置。")
        raise ValueError("missing host")
    print("接下来输入该服务现有的密码或 Token，不会创建或更改远端账号。")
    password = read_hidden_secret("密码 / Token（隐藏输入，输入后回车）：")
    confirmation = read_hidden_secret("再次输入密码 / Token：")
    if password != confirmation:
        print("两次输入不一致；未保存。")
        raise ValueError("password mismatch")
    if input("保存此服务及密码？[y/N]：").strip().lower() != "y":
        print("已取消，未保存。")
        return
    updated_inventory = ServicesConfig(services=[*inventory.services, service])
    lines = ["schema_version = 1", ""]
    for saved_service in updated_inventory.services:
        lines.append("[[services]]")
        lines.extend(
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in saved_service.model_dump().items()
        )
        lines.append("")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".services-", dir=private_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as metadata_file:
            metadata_file.write("\n".join(lines))
        # This also backs up existing service metadata before either file changes.
        store_secret(private_dir / "secrets.env", reference, password)
        temporary_path.replace(private_dir / "services.toml")
    finally:
        temporary_path.unlink(missing_ok=True)
    print("服务已保存，密码已关联且未显示。运行 just service-list 查看。")


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
        credentials = dotenv_values(
            arguments.private_dir / "secrets.env", interpolate=False, encoding="utf-8"
        )
        summaries = [
            {
                **service.model_dump(),
                "password_configured": bool(credentials.get(service.password_env)),
            }
            for service in selected
        ]
        if not summaries:
            print("尚未登记服务。运行 just service-add 添加。")
        else:
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
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
