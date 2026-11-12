"""Store local credentials or inject them into a command without displaying values."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from zata_ops.private_credentials import (
    SECRET_NAME,
    credential_status,
    store_secret,
    validate_private_directory,
)

REDACTION_MARKER = "***"
# 过短的凭据做字面替换会误伤普通输出，也无法可信地屏蔽，因此拒绝放开输出。
MIN_REDACTABLE_LENGTH = 8


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


def redact_output(output_text: str, secret_values: list[str]) -> str:
    """Replace every literal occurrence of each credential value.

    Args:
        output_text: Child output decoded as text.
        secret_values: Injected credential values, longest replaced first.

    Returns:
        Output with each value replaced by the redaction marker.
    """
    redacted_text = output_text
    for secret_value in sorted(secret_values, key=len, reverse=True):
        redacted_text = redacted_text.replace(secret_value, REDACTION_MARKER)
    return redacted_text


def run_with_credentials(
    secret_path: Path,
    bindings: dict[str, str],
    command: list[str],
    *,
    working_directory: Path | None = None,
    show_redacted_output: bool = False,
) -> int:
    """Inject selected credentials and suppress or redact child output.

    Args:
        secret_path: Private dotenv file.
        bindings: Destination environment names mapped to stored references.
        command: Command argument vector, without shell evaluation.
        working_directory: Directory the child runs in; defaults to the caller's.
        show_redacted_output: Print child stdout/stderr with injected values
            replaced by ``***`` instead of suppressing it. Literal matching only:
            a child that re-encodes a value (base64, URL escaping, splitting) can
            still leak it, so use this only with trusted commands.

    Returns:
        Child exit status, normalized to one for termination by signal.

    Raises:
        ValueError: Command, environment name, credential or directory is missing,
            or a credential is too short to redact reliably.
        OSError: Process cannot start or credentials cannot be read.
    """
    credentials = dotenv_values(secret_path, interpolate=False, encoding="utf-8")
    if any(not credentials.get(reference) for reference in bindings.values()):
        raise ValueError("missing credential")
    return run_with_environment(
        {name: credentials[reference] for name, reference in bindings.items()},
        command,
        ExecutionOptions(working_directory, show_redacted_output),
    )


@dataclass(frozen=True)
class ExecutionOptions:
    """Control the child working directory and optional literal output redaction."""

    working_directory: Path | None = None
    show_redacted_output: bool = False


def run_with_environment(
    selected_environment: dict[str, str],
    command: list[str],
    options: ExecutionOptions,
) -> int:
    """Execute a command with selected values and controlled output.

    Args:
        selected_environment: Validated destination names and literal values.
        command: Argument vector, executed without a shell.
        options: Working directory and output policy.

    Returns:
        Child exit code, or one for signal termination.

    Raises:
        ValueError: Invalid environment, directory, command or redaction length.
        OSError: Child cannot start.
    """
    if not command or any(
        not SECRET_NAME.fullmatch(name) or "\x00" in value
        for name, value in selected_environment.items()
    ):
        raise ValueError("invalid command or environment")
    working_directory = options.working_directory
    show_redacted_output = options.show_redacted_output
    if working_directory is not None and not working_directory.is_dir():
        raise ValueError("working directory does not exist")
    injected_values = list(selected_environment.values())
    if show_redacted_output and any(
        len(injected_value) < MIN_REDACTABLE_LENGTH
        for injected_value in injected_values
    ):
        raise ValueError("credential too short to redact")
    process_environment = os.environ.copy()
    process_environment.update(selected_environment)
    if not show_redacted_output:
        completed = subprocess.run(
            command,
            env=process_environment,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Command finished; exit={completed.returncode}; output suppressed.")
        return completed.returncode if completed.returncode >= 0 else 1
    completed = subprocess.run(
        command,
        env=process_environment,
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    child_output = completed.stdout.decode("utf-8", errors="replace")
    print(redact_output(child_output, injected_values), end="")
    print(f"Command finished; exit={completed.returncode}; values redacted.")
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
        "run", help="Inject named credentials; suppress or redact child output"
    )
    run_parser.add_argument("--env", action="append", required=True, dest="names")
    run_parser.add_argument(
        "--cwd", type=Path, default=None, help="Directory the command runs in"
    )
    run_parser.add_argument(
        "--show-output",
        action="store_true",
        help="Print child output with injected values replaced by ***",
    )
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
        if arguments.action == "list":
            credential_names = sorted(credential_status(secret_path))
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
            secret_path,
            {name: name for name in names},
            command,
            working_directory=arguments.cwd,
            show_redacted_output=arguments.show_output,
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
