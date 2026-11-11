"""把本地数据库备份恢复到测试 Postgres 的临时库，并用 Adminer 浏览；用完再清理。

``up`` 用 ``zata-ops db restore --local-dir`` 恢复（同时是一次恢复演练），为 dump 中
``OWNER TO`` 引用但本地不存在的角色建 NOLOGIN 占位角色并记入状态文件；``down`` 删除
临时库与这些角色。只连接本机测试中间件，不访问任何服务器。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from zata_ops.db._local_store import LocalBackupStore

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(REPO_ROOT / "local/docker-compose.testing.yml"),
]
PG_CONTAINER = "test-postgres"
PG_USER, PG_PASSWORD = "testuser", "testpass"
OWNER_PATTERN = re.compile(r'OWNER TO ("?)([A-Za-z_][A-Za-z0-9_$-]*)\1;')


def _psql(database: str, sql: str) -> str:
    """在测试 Postgres 容器内执行一条 SQL。

    Args:
        database: 目标数据库。
        sql: 要执行的语句。

    Returns:
        去掉首尾空白的输出。
    """
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={PG_PASSWORD}",
            PG_CONTAINER,
            "psql",
            "-h",
            "localhost",
            "-U",
            PG_USER,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _quote_ident(name: str) -> str:
    """返回 PostgreSQL 双引号标识符。"""
    return '"' + name.replace('"', '""') + '"'


def _pg_client_dir() -> str:
    """返回包含 ``psql`` 的目录，优先使用 Homebrew 的 libpq；系统已有时为空字符串。"""
    if shutil.which("brew"):
        brew_prefix = subprocess.run(
            ["brew", "--prefix", "libpq"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if brew_prefix and (Path(brew_prefix) / "bin" / "psql").exists():
            return str(Path(brew_prefix) / "bin")
    if shutil.which("psql"):
        return ""
    raise SystemExit(
        "psql not found; install the client tools first: brew install libpq"
    )


def _state_path(local_dir: Path, prefix: str) -> Path:
    """返回记录临时库与占位角色的状态文件路径。"""
    return local_dir / f".backup-view-{prefix}.json"


def view_up(local_dir: Path, prefix: str, backup_date: str | None) -> int:
    """恢复备份到临时库并打印 Adminer 链接。

    Args:
        local_dir: 本地备份根目录。
        prefix: 备份前缀，例如 ``keycloak``。
        backup_date: 备份时间；为空时使用最新一份。

    Returns:
        进程退出码。
    """
    store = LocalBackupStore(local_dir, prefix)
    available_dates = store.list_backup_dates()
    if not available_dates:
        raise SystemExit(f"no local backups under {local_dir / prefix}")
    target_date = backup_date or available_dates[-1]
    manifest = store.get_backup_manifest(target_date)
    if not manifest:
        raise SystemExit(f"backup {target_date} not found under {local_dir / prefix}")
    dump_key = next(
        entry["s3_key"]
        for entry in manifest["files"]
        if entry["name"] == "database.sql.gz"
    )
    if _state_path(local_dir, prefix).exists():
        view_down(local_dir, prefix)

    client_dir = _pg_client_dir()
    subprocess.run([*COMPOSE, "up", "-d", "postgres", "adminer"], check=True)
    for _ in range(30):
        if (
            subprocess.run(
                ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", PG_USER],
                capture_output=True,
            ).returncode
            == 0
        ):
            break
        time.sleep(1)

    view_db = re.sub(r"[^a-z0-9_]", "_", prefix.lower()) + "_backup_view"
    with gzip.open(local_dir / dump_key, "rt", encoding="utf-8") as dump_reader:
        owner_roles = sorted(
            {
                match.group(2)
                for line in dump_reader
                for match in OWNER_PATTERN.finditer(line)
            }
        )
    existing_roles = set(_psql("postgres", "select rolname from pg_roles").splitlines())
    created_roles = [role for role in owner_roles if role not in existing_roles]
    _psql("postgres", f"drop database if exists {_quote_ident(view_db)}")
    _psql("postgres", f"create database {_quote_ident(view_db)}")
    for role in created_roles:
        _psql("postgres", f"create role {_quote_ident(role)} nologin")
    _state_path(local_dir, prefix).write_text(
        json.dumps({"database": view_db, "created_roles": created_roles}),
        encoding="utf-8",
    )

    restore_env_path = f"{client_dir}:" if client_dir else ""
    restore_returncode = subprocess.run(
        [
            "sh",
            "-c",
            f'PATH="{restore_env_path}$PATH" exec "$@"',
            "restore",
            "uv",
            "run",
            "zata-ops",
            "db",
            "restore",
            "--from",
            target_date,
            "--local-dir",
            str(local_dir),
            "--s3-prefix",
            prefix,
            "--target-db-url",
            f"postgresql://{PG_USER}:{PG_PASSWORD}@localhost:5432/{view_db}",
            "--restore-db",
            "--yes",
        ],
        cwd=REPO_ROOT,
        check=False,
    ).returncode
    if restore_returncode != 0:
        return restore_returncode
    print(f"\nBackup {target_date} restored into {view_db}.")
    print(
        f"Open: http://127.0.0.1:8081/?pgsql={PG_CONTAINER}&username={PG_USER}&db={view_db}&ns=public"
    )
    print(f"Password: {PG_PASSWORD}  (local test database only)")
    print(
        "When done: just db-backup-view-down"
        + ("" if prefix == "keycloak" else f" {prefix}")
    )
    return 0


def view_down(local_dir: Path, prefix: str) -> int:
    """删除临时库与 ``up`` 创建的占位角色。

    Args:
        local_dir: 本地备份根目录。
        prefix: 备份前缀。

    Returns:
        进程退出码。
    """
    state_path = _state_path(local_dir, prefix)
    if not state_path.exists():
        print("nothing to clean up")
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _psql("postgres", f"drop database if exists {_quote_ident(state['database'])}")
    for role in state["created_roles"]:
        _psql("postgres", f"drop role if exists {_quote_ident(role)}")
    state_path.unlink()
    print(
        f"dropped {state['database']} and {len(state['created_roles'])} placeholder role(s)"
    )
    return 0


def main() -> int:
    """解析参数并执行 ``up`` 或 ``down``。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "down"])
    parser.add_argument(
        "prefix", nargs="?", default="keycloak", help="备份前缀，默认 keycloak"
    )
    parser.add_argument(
        "--from", dest="backup_date", help="备份时间 YYYY-MM-DD_HHMMSS，默认最新"
    )
    parser.add_argument("--local-dir", default="config/private/backups", type=Path)
    options = parser.parse_args()
    local_dir = (REPO_ROOT / options.local_dir).resolve()
    if options.action == "up":
        return view_up(local_dir, options.prefix, options.backup_date)
    return view_down(local_dir, options.prefix)


if __name__ == "__main__":
    sys.exit(main())
