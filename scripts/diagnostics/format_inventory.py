"""Check or canonically format inventory without printing private configuration."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import tomllib
from pathlib import Path

from check_inventory import validate_private_inventory
from private_config_backup import create_backup
from zata_ops.inventory import InventoryConfig, load_inventory

EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "config/servers.example.toml"


def _render_field(name: str, value: object, indent: str) -> list[str]:
    if name != "notes" or not isinstance(value, str) or len(value) <= 76:
        return [f"{indent}{name} = {json.dumps(value, ensure_ascii=False)}"]
    rendered_lines = [indent + 'notes = """\\']
    for offset in range(0, len(value), 66):
        escaped_chunk = json.dumps(value[offset : offset + 66], ensure_ascii=False)[
            1:-1
        ]
        # TOML continuations trim indentation; escape only meaningful leading spaces.
        leading_spaces = len(escaped_chunk) - len(escaped_chunk.lstrip(" "))
        rendered_lines.append(
            indent
            + "  "
            + "\\u0020" * leading_spaces
            + escaped_chunk[leading_spaces:]
            + "\\"
        )
    return [*rendered_lines, indent + '"""']


def render_inventory(inventory: InventoryConfig) -> str:
    """Render server / SSH / application hierarchy with lossless note wrapping.

    Args:
        inventory: Validated inventory; order and field values are preserved.

    Returns:
        Canonical UTF-8 TOML text. Custom comments are replaced with generated headings.

    Raises:
        ValueError: Rendered content differs semantically from the input.
    """
    inventory_payload = inventory.model_dump(mode="json")
    rendered_lines = [
        "# 运维清单：服务器 → SSH / 应用。真实配置仅存本机私有目录。",
        "# 分节标题由 inventory-format 生成；需要保留的信息请写入 notes。",
        f"schema_version = {inventory.schema_version}",
        f"updated_at = {json.dumps(inventory.updated_at, ensure_ascii=False)}",
    ]
    for server_number, server in enumerate(inventory_payload["servers"], start=1):
        title = f"服务器 {server_number}：{server['name']} ｜ {server['region']} ｜ 应用 {len(server['applications'])} 项"
        rendered_lines.extend(
            [
                "",
                "# " + "═" * 72,
                "# " + title.replace("\n", " ").replace("\r", " "),
                "# " + "═" * 72,
                "[[servers]]",
            ]
        )
        for field_name, field_value in server.items():
            if field_name not in {"ssh", "applications"}:
                rendered_lines.extend(_render_field(field_name, field_value, "  "))
        if not server["applications"]:
            rendered_lines.append("  applications = []")
        rendered_lines.extend(["", "  # ── SSH 连接 ──", "  [servers.ssh]"])
        for field_name, field_value in server["ssh"].items():
            rendered_lines.extend(_render_field(field_name, field_value, "    "))
        for app_number, application in enumerate(server["applications"], start=1):
            app_title = f"  # ── 应用 {app_number}：{application['name']} ──".replace(
                "\n", " "
            ).replace("\r", " ")
            rendered_lines.extend(["", app_title, "  [[servers.applications]]"])
            for field_name, field_value in application.items():
                rendered_lines.extend(_render_field(field_name, field_value, "    "))
    rendered_text = "\n".join(rendered_lines) + "\n"
    # Verify TOML parsing as well as model validation; never silently normalize values.
    if tomllib.loads(rendered_text) != inventory_payload:
        raise ValueError("formatting changed inventory values")
    return rendered_text


def main() -> int:
    """Check by default, or atomically write after validating and backing up.

    Returns:
        Zero for canonical/written content, one if formatting is needed, two on error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", type=Path, default=Path("config/private/servers.toml")
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Back up private configuration and write canonical layout",
    )
    parser.add_argument(
        "--example",
        action="store_true",
        help="Skip privacy checks; writes restricted to the shipped public example",
    )
    arguments = parser.parse_args()
    temporary_path = None
    try:
        if arguments.example:
            if any(
                parent.is_symlink()
                for parent in (
                    arguments.path.absolute(),
                    *arguments.path.absolute().parents,
                )
            ):
                raise ValueError("symlink example")
            if arguments.write and arguments.path.resolve() != EXAMPLE_PATH:
                raise ValueError("example writes restricted to public example")
        else:
            validate_private_inventory(arguments.path)
        original_text = arguments.path.read_text(encoding="utf-8")
        formatted_text = render_inventory(load_inventory(arguments.path))
        if tomllib.loads(original_text) != tomllib.loads(formatted_text):
            raise ValueError("source values would change")
        if original_text == formatted_text:
            print("Inventory format valid; no changes made.")
            return 0
        if not arguments.write:
            print(
                "Inventory needs formatting; rerun with --write. No values printed or files changed."
            )
            return 1
        if not arguments.example:
            create_backup(arguments.path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=arguments.path.parent,
            prefix=".inventory-",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            if arguments.example:
                temporary_path.chmod(arguments.path.stat().st_mode & 0o777)
            temporary_file.write(formatted_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(arguments.path)
        print(
            "Inventory formatted; parsed values unchanged."
            + (" Private configuration backed up." if not arguments.example else "")
        )
        return 0
    except (OSError, ValueError):
        print(
            "Inventory formatting failed; check schema, file permissions, Git exclusion and symlinks. Values withheld."
        )
        return 2
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
