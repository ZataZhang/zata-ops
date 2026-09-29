"""把官方 E2B Embed SDK 环境值写入应用本地环境文件。"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

_SDK_KEYS = ("E2B_API_KEY", "E2B_API_URL", "E2B_SANDBOX_URL")
_APP_SETTINGS = {
    "SANDBOX_AGENT_PROVIDER": "e2b",
}


def main() -> int:
    """读取官方 SDK 导出值并原子更新目标环境文件。"""
    if len(sys.argv) != 3 or not sys.argv[2].strip():
        print(
            "Usage: e2b_embed_configure.py <application-env-file> <template-id>",
            file=sys.stderr,
        )
        return 2

    target_path = Path(sys.argv[1])
    template_id = sys.argv[2].strip()
    sdk_values: dict[str, str] = {}
    for sdk_line in sys.stdin:
        matched_export = re.fullmatch(r"export\s+([A-Z0-9_]+)=(.*)", sdk_line.strip())
        if matched_export is None or matched_export.group(1) not in _SDK_KEYS:
            continue
        parsed_value = shlex.split(matched_export.group(2))
        if parsed_value:
            setting_name = matched_export.group(1)
            setting_value = parsed_value[0]
            if setting_name in {"E2B_API_URL", "E2B_SANDBOX_URL"}:
                setting_value = setting_value.replace(
                    "://localhost:", "://127.0.0.1:", 1
                )
            sdk_values[setting_name] = setting_value

    missing_sdk_keys = [sdk_key for sdk_key in _SDK_KEYS if not sdk_values.get(sdk_key)]
    if missing_sdk_keys:
        print(
            "Official Embed SDK configuration is missing: "
            + ", ".join(missing_sdk_keys),
            file=sys.stderr,
        )
        return 1

    merged_values = {
        **sdk_values,
        **_APP_SETTINGS,
        "E2B_TEMPLATE_ID": template_id,
    }
    current_text = (
        target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    )
    replaced_settings: set[str] = set()
    kept_lines: list[str] = []
    settings_pattern = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
    for current_line in current_text.splitlines():
        matched_setting = settings_pattern.match(current_line)
        setting_name = matched_setting.group(1) if matched_setting else ""
        if setting_name not in merged_values:
            kept_lines.append(current_line)
        elif setting_name not in replaced_settings:
            kept_lines.append(f"{setting_name}={merged_values[setting_name]}")
            replaced_settings.add(setting_name)

    missing_lines = [
        f"{setting_name}={setting_value}"
        for setting_name, setting_value in merged_values.items()
        if setting_name not in replaced_settings
    ]
    if missing_lines:
        if kept_lines and kept_lines[-1].strip():
            kept_lines.append("")
        kept_lines.extend(["# E2B Embed local development settings", *missing_lines])

    updated_text = "\n".join(kept_lines).rstrip() + "\n"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", dir=target_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as env_file_handle:
            env_file_handle.write(updated_text)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(target_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"Configured local E2B Embed settings in {target_path} (key value hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
