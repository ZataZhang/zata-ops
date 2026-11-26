"""Run trusted subprocesses with explicitly selected credentials."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from zata_ops.services import SECRET_NAME

REDACTION_MARKER = "***"
MIN_REDACTABLE_LENGTH = 8
SYSTEM_ENV = frozenset(
    {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
)


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


@dataclass(frozen=True)
class ExecutionOptions:
    """Control the child working directory and optional literal output redaction."""

    working_directory: Path | None = None
    show_redacted_output: bool = False
    inherit_environment: bool = True


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
    if (
        os.name == "nt"
        and command
        and Path(command[0]).suffix.lower() in {".bat", ".cmd"}
        and not options.inherit_environment
    ):
        raise ValueError("remote execution requires a native executable")
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
    process_environment = (
        os.environ.copy()
        if options.inherit_environment
        else {
            key: value for key, value in os.environ.items() if key.upper() in SYSTEM_ENV
        }
    )
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
