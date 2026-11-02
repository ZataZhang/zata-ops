"""Create local private-config snapshots and remind users to copy them elsewhere."""

from __future__ import annotations

import argparse
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    """Run an explicit backup, reminder check, or manual-copy acknowledgement.

    Returns:
        Zero on success; one on file or configuration errors.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "remind", "acknowledge"])
    parser.add_argument("--private-dir", type=Path, default=Path("config/private"))
    arguments = parser.parse_args()
    try:
        if arguments.action == "backup":
            archive_path = create_backup(arguments.private_dir)
            print(f"本机备份已保存：{archive_path}（保留最近 {KEEP_BACKUPS} 份）")
        else:
            remind_manual_copy(
                arguments.private_dir, acknowledge=arguments.action == "acknowledge"
            )
    except (OSError, ValueError, tarfile.TarError) as backup_error:
        print(
            f"操作失败（{type(backup_error).__name__}）；请检查私有目录、权限和提醒状态。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
