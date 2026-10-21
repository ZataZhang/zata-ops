"""Validate local server inventory without connecting to servers or printing secrets."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from pydantic import ValidationError

from zata_ops.inventory import load_inventory


def main() -> int:
    """Validate a TOML inventory and optional Git privacy boundaries.

    Returns:
        Zero on success, one on invalid configuration.
    """
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "path", nargs="?", default="config/private/servers.toml"
    )
    argument_parser.add_argument(
        "--example", action="store_true", help="Skip private-path Git checks"
    )
    cli_arguments = argument_parser.parse_args()
    inventory_path = Path(cli_arguments.path)
    try:
        inventory_config = load_inventory(inventory_path)
        if not cli_arguments.example:
            tracked_check = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", str(inventory_path)],
                capture_output=True,
            )
            ignored_check = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", str(inventory_path)],
                capture_output=True,
            )
            if tracked_check.returncode != 1 or ignored_check.returncode != 0:
                raise ValueError("private inventory must be Git-ignored and untracked")
    except (OSError, ValueError, ValidationError) as validation_error:
        # Do not echo TOML contents or Pydantic input values.
        if isinstance(validation_error, ValidationError):
            for field_error in validation_error.errors(include_input=False):
                print(
                    f"Invalid field: {'.'.join(map(str, field_error['loc']))} ({field_error['type']})"
                )
        else:
            print(
                f"Inventory validation failed ({type(validation_error).__name__}); check file, TOML syntax and Git exclusion"
            )
        return 1
    print(
        f"Inventory valid: {len(inventory_config.servers)} server(s); no SSH connections made"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
