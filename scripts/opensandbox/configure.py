"""Create a private, host-specific OpenSandbox profile without showing its key."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

from configuration import DATA_ROOT, normalize_data_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_TEMPLATE = REPOSITORY_ROOT / "deploy/opensandbox/config.example.toml"
PRIVATE_ROOT = REPOSITORY_ROOT / "config/private"


def create_private_profile(output_directory: Path, data_root: Path) -> dict[str, str]:
    """Create one ignored local profile and generate a fresh 256-bit API key.

    Args:
        output_directory: New directory below config/private.
        data_root: Dedicated target-host storage path ending in opensandbox.

    Returns:
        Non-secret paths and status for display.

    Raises:
        OSError: The private files cannot be created.
        ValueError: The output path is public, exists, or the storage path is unsafe.
    """
    normalized_data_root = normalize_data_root(data_root)
    absolute_output = output_directory.resolve(strict=False)
    if not absolute_output.is_relative_to(PRIVATE_ROOT.resolve()):
        raise ValueError("output_must_be_under_config_private")
    if output_directory.exists() or output_directory.is_symlink():
        raise ValueError("output_directory_already_exists")
    ignored_path = subprocess.run(
        ["git", "check-ignore", "-q", str(absolute_output)],
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=10,
    )
    if ignored_path.returncode != 0:
        raise ValueError("output_directory_is_not_git_ignored")
    rendered_config = PROFILE_TEMPLATE.read_text(encoding="utf-8")
    if str(DATA_ROOT) not in rendered_config:
        raise ValueError("profile_template_data_root_missing")
    rendered_config = rendered_config.replace(str(DATA_ROOT), str(normalized_data_root))
    server_key = secrets.token_hex(32)
    rendered_environment = (
        "# Generated locally. Do not source this file or copy it into Git.\n"
        f"OPENSANDBOX_SERVER_API_KEY={server_key}\n"
    )
    output_directory.mkdir(mode=0o700, parents=True)
    output_directory.chmod(0o700)
    created_files = []
    try:
        for file_name, file_content in (
            ("config.toml", rendered_config),
            ("server.env", rendered_environment),
        ):
            output_path = output_directory / file_name
            with output_path.open("x", encoding="utf-8") as output_file:
                output_file.write(file_content)
                output_file.flush()
            output_path.chmod(0o600)
            created_files.append(output_path)
    except OSError:
        for created_path in created_files:
            created_path.unlink(missing_ok=True)
        output_directory.rmdir()
        raise
    return {
        "status": "created",
        "config": str(output_directory / "config.toml"),
        "env_file": str(output_directory / "server.env"),
        "data_root": str(normalized_data_root),
        "api_key": "generated_and_not_displayed",
    }


def main() -> int:
    """Create local private files and print only non-secret metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=DATA_ROOT, help="Absolute target-host path"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PRIVATE_ROOT / "opensandbox",
        help="New private directory; must be under config/private",
    )
    arguments = parser.parse_args()
    try:
        profile_status = create_private_profile(
            arguments.output_dir, arguments.data_root
        )
    except (OSError, ValueError) as profile_error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": type(profile_error).__name__,
                    "detail": str(profile_error)
                    if isinstance(profile_error, ValueError)
                    else "private_file_creation_failed",
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(profile_status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
