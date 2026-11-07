"""Create local private-config snapshots and remind users to copy them elsewhere."""

from __future__ import annotations

import argparse
import tarfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zata_ops.private_config_archive import (
    RestoreRollbackError,
    RestoreResult,
    restore_backup,
)
from zata_ops.private_config_backup import KEEP_BACKUPS, create_backup

REMINDER_DAYS = 7


def remind_manual_copy(private_dir: Path, *, acknowledge: bool) -> None:
    """Remind users of a manual copy, or acknowledge their completed copy.

    Args:
        private_dir: Directory containing configuration and reminder state.
        acknowledge: Whether the user confirms they completed a manual copy.

    Returns:
        None.

    Raises:
        OSError: Reminder state cannot be read or written.
        ValueError: Directory or reminder state is invalid.
    """
    if private_dir.is_symlink() or not private_dir.is_dir():
        raise ValueError("private configuration directory is missing or is a symlink")
    marker_path = private_dir / ".copy-reminder"
    if marker_path.is_symlink():
        raise ValueError("reminder state must not be a symlink")
    current_time = datetime.now(timezone.utc)
    if acknowledge:
        marker_path.touch(mode=0o600, exist_ok=True)
        marker_path.chmod(0o600)
        marker_path.write_text(current_time.isoformat(), encoding="utf-8")
        print("已记录你完成手动复制；7 天后再次提醒。此命令本身不会复制配置。")
        return
    last_copy_time = (
        datetime.fromisoformat(marker_path.read_text(encoding="utf-8").strip())
        if marker_path.exists()
        else None
    )
    if last_copy_time is not None and last_copy_time.tzinfo is None:
        raise ValueError("reminder timestamp must include timezone")
    if last_copy_time is None or current_time - last_copy_time >= timedelta(
        days=REMINDER_DAYS
    ):
        print(
            "提醒：尚未记录手动复制，或距上次已满 7 天。请将 config/private/ 手动复制到你选择的其他位置。"
        )
        print("复制完成后运行 just private-copy-done。仅生成本机备份不会清除此提醒。")
    else:
        print("手动复制提醒：距上次确认未满 7 天。")


def main() -> int:
    """Run a backup, restore preview, restore, or manual-copy reminder action.

    Returns:
        Zero on success; one on file or configuration errors.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    action_parsers = parser.add_subparsers(dest="action", required=True)

    backup_parser = action_parsers.add_parser("backup")
    backup_parser.add_argument(
        "--private-dir", type=Path, default=Path("config/private")
    )

    restore_parser = action_parsers.add_parser("restore")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument(
        "--apply",
        action="store_true",
        help="replace active private files after creating a recovery backup",
    )
    restore_parser.add_argument(
        "--merge",
        action="store_true",
        help="add files that do not exist and preserve existing destination paths",
    )

    for reminder_action in ("remind", "acknowledge"):
        reminder_parser = action_parsers.add_parser(reminder_action)
        reminder_parser.add_argument(
            "--private-dir", type=Path, default=Path("config/private")
        )

    parsed_arguments = parser.parse_args()
    try:
        if parsed_arguments.action == "backup":
            archive_path = create_backup(parsed_arguments.private_dir)
            print(f"本机备份已保存：{archive_path}（保留最近 {KEEP_BACKUPS} 份）")
        elif parsed_arguments.action == "restore":
            restore_result: RestoreResult = restore_backup(
                parsed_arguments.archive,
                Path("config/private"),
                apply_restore=parsed_arguments.apply,
                merge_existing=parsed_arguments.merge,
            )
            print(
                f"归档有效：{len(restore_result.member_paths)} 个文件，"
                f"解压后 {restore_result.total_size_bytes} 字节。"
            )
            skipped_member_path_set = set(restore_result.skipped_member_paths)
            for member_path in restore_result.member_paths:
                if member_path in skipped_member_path_set:
                    action_label = "保留现有"
                elif restore_result.merge_existing:
                    action_label = "导入"
                else:
                    action_label = "恢复"
                print(f"  {action_label}: {member_path}")
            if parsed_arguments.apply and restore_result.applied:
                operation_label = "导入" if restore_result.merge_existing else "恢复"
                print(
                    f"私有配置已{operation_label}；恢复前备份："
                    f"{restore_result.recovery_backup_path or '无现有配置文件'}。"
                )
                print(
                    "backups/ 与 .copy-reminder 已保留，文件权限设为 600、目录权限设为 700。"
                )
            elif parsed_arguments.apply:
                print("没有导入文件：归档中的路径已存在，未修改配置或创建备份。")
            else:
                print("仅完成预览，没有修改私有配置。应用操作请增加 --apply。")
        else:
            remind_manual_copy(
                parsed_arguments.private_dir,
                acknowledge=parsed_arguments.action == "acknowledge",
            )
    except RestoreRollbackError as restore_error:
        print(
            "恢复失败且自动回滚未完成；暂存文件保留在 "
            f"{restore_error.staging_directory}。"
        )
        return 1
    except (
        OSError,
        ValueError,
        EOFError,
        zlib.error,
        tarfile.TarError,
    ) as backup_error:
        print(
            f"操作失败（{type(backup_error).__name__}）；请检查归档、私有目录和权限。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
