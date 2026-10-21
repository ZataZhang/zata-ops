"""Store local credentials or inject them into a command without displaying values."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from dotenv import dotenv_values, set_key
from private_config_backup import create_backup


SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def read_hidden_secret(prompt: str) -> str:
    """Read a secret without falling back to echoed input.

    Args:
        prompt: Explanation displayed before hidden input.

    Returns:
        User-entered value.

    Raises:
        ValueError: Input is not an interactive terminal.
        getpass.GetPassWarning: Hidden input is unavailable.
    """
    if not sys.stdin.isatty():
        raise ValueError("hidden input requires a terminal")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def validate_private_directory(private_dir: Path) -> None:
    """Require an ignored, untracked location within this checkout.

    Args:
        private_dir: Credential directory, resolved relative to the current directory.

    Returns:
        None.

    Raises:
        ValueError: Path is tracked, outside the checkout, or a symbolic link.
        OSError: Git is unavailable or the directory cannot be created.
    """
    repo_root = Path(__file__).resolve().parents[2]
    absolute_dir = private_dir.absolute()
    if any(parent.is_symlink() for parent in (absolute_dir, *absolute_dir.parents)):
        raise ValueError("symlink path")
    relative_dir = absolute_dir.relative_to(repo_root)
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(relative_dir / "secrets.env")],
        cwd=repo_root,
        capture_output=True,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(relative_dir)],
        cwd=repo_root,
        capture_output=True,
    )
    if ignored.returncode != 0 or tracked.returncode != 0 or tracked.stdout:
        raise ValueError("private directory must be ignored and untracked")
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_dir.chmod(0o700)
    secret_path = private_dir / "secrets.env"
    if secret_path.is_symlink():
        raise ValueError("symlink credential file")
    if secret_path.exists():
        secret_path.chmod(0o600)


def store_secret(secret_path: Path, name: str, value: str) -> None:
    """Back up existing configuration and atomically update a credential.

    Args:
        secret_path: Local dotenv file.
        name: Validated environment variable name.
        value: Single-line credential to store.

    Returns:
        None.

    Raises:
        OSError: Backup or atomic write fails.
        ValueError: Value is empty or contains unsupported characters.
    """
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("invalid credential value")
    private_dir = secret_path.parent
    existing_files = [
        config_path
        for config_path in private_dir.iterdir()
        if config_path.name not in {"backups", ".copy-reminder"}
    ]
    if existing_files:
        create_backup(private_dir)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secret-", dir=private_dir
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as credential_file:
            if secret_path.exists():
                credential_file.write(secret_path.read_text(encoding="utf-8"))
        set_key(temporary_path, name, value, quote_mode="always", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(secret_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_with_credentials(
    secret_path: Path, bindings: dict[str, str], command: list[str]
) -> int:
    """Inject selected credentials and suppress child output.

    Args:
        secret_path: Private dotenv file.
        bindings: Destination environment names mapped to stored references.
        command: Command argument vector, without shell evaluation.

    Returns:
        Child exit status, normalized to one for termination by signal.

    Raises:
        ValueError: Command, environment name, or credential is missing.
        OSError: Process cannot start or credentials cannot be read.
    """
    credentials = dotenv_values(secret_path, interpolate=False, encoding="utf-8")
    if not command or any(
        not SECRET_NAME.fullmatch(name) or not credentials.get(reference)
        for name, reference in bindings.items()
    ):
        raise ValueError("missing command or credential")
    process_environment = os.environ.copy()
    process_environment.update(
        {name: credentials[reference] for name, reference in bindings.items()}
    )
    completed = subprocess.run(
        command,
        env=process_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Command finished; exit={completed.returncode}; output suppressed.")
    return completed.returncode if completed.returncode >= 0 else 1


def main() -> int:
    """Dispatch credential operations without printing values or child output.

    Returns:
        Zero on success, one on input/storage errors, or the child exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, default=Path("config/private"))
    commands = parser.add_subparsers(dest="action", required=True)
    set_parser = commands.add_parser("set", help="User enters a hidden value")
    set_parser.add_argument("name")
    set_parser.add_argument(
        "--stdin", action="store_true", help="Read from a trusted pipe"
    )
    commands.add_parser("list", help="List names only")
    run_parser = commands.add_parser(
        "run", help="Inject named credentials; suppress child output"
    )
    run_parser.add_argument("--env", action="append", required=True, dest="names")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    try:
        validate_private_directory(arguments.private_dir)
        secret_path = arguments.private_dir / "secrets.env"
        names = (
            [arguments.name]
            if arguments.action == "set"
            else getattr(arguments, "names", [])
        )
        if any(not SECRET_NAME.fullmatch(name) for name in names):
            raise ValueError("invalid credential name")
        if arguments.action == "set":
            if arguments.stdin:
                secret_value = sys.stdin.read().removesuffix("\n")
            else:
                secret_value = read_hidden_secret("Credential (hidden): ")
            store_secret(secret_path, arguments.name, secret_value)
            print("Credential saved; value hidden.")
            return 0
        # Disable dotenv expansion so passwords containing ${...} stay literal.
        credentials = dotenv_values(secret_path, interpolate=False, encoding="utf-8")
        if arguments.action == "list":
            credential_names = sorted(
                name for name in credentials if SECRET_NAME.fullmatch(name)
            )
            print(
                "\n".join(credential_names)
                if credential_names
                else "尚未保存凭据。登记数据库或服务请运行 just service-add。"
            )
            return 0
        command = arguments.command
        if command[:1] == ["--"]:
            command = command[1:]
        return run_with_credentials(
            secret_path, {name: name for name in names}, command
        )
    except (OSError, ValueError, getpass.GetPassWarning):
        print(
            "Credential operation failed; check name, input, private path and command. Values hidden.",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Credential operation cancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
