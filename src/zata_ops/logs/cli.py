"""Typer commands for inspecting Docker and systemd logs."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

_PROJECT_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RELATIVE_DURATION_PATTERN = re.compile(r"^(?P<amount>[0-9]+)(?P<unit>[smhd])$")
_JOURNAL_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


def _build_source_command(
    *,
    project: Optional[str],
    unit: Optional[str],
    since: str,
    follow: bool,
) -> tuple[str, list[str]]:
    """Build an argv list for exactly one Docker project or systemd unit.

    Args:
        project: Project slug whose ``<project>-backend`` container is inspected.
        unit: systemd unit name to inspect.
        since: Start time for the requested log window.
        follow: Whether to keep streaming new entries.

    Returns:
        The source name and argv list for its log command.

    Raises:
        typer.BadParameter: If neither or both log sources are selected, or a
            project slug is invalid.
    """
    if (project is None) == (unit is None):
        raise typer.BadParameter("provide exactly one of --project or --unit")

    if project is not None:
        if not _PROJECT_SLUG_PATTERN.fullmatch(project):
            raise typer.BadParameter("project must be a valid container-name prefix")
        docker_argv_list = ["docker", "logs", f"--since={since}"]
        if follow:
            docker_argv_list.append("--follow")
        docker_argv_list.append(f"{project}-backend")
        return "docker", docker_argv_list

    if not unit or any(character.isspace() for character in unit):
        raise typer.BadParameter("unit must be a non-empty systemd unit name")

    duration_match = _RELATIVE_DURATION_PATTERN.fullmatch(since)
    if duration_match is None:
        journal_since = since
    else:
        amount_text = duration_match.group("amount")
        duration_unit = _JOURNAL_DURATION_UNITS[duration_match.group("unit")]
        journal_since = f"{amount_text} {duration_unit} ago"

    journalctl_argv_list = [
        "journalctl",
        f"--unit={unit}",
        f"--since={journal_since}",
        "--no-pager",
    ]
    if follow:
        journalctl_argv_list.append("--follow")
    return "systemd", journalctl_argv_list


@app.command("tail")
def tail_command(
    project: Optional[str] = typer.Option(
        None, help="Docker project slug; reads its <project>-backend container."
    ),
    unit: Optional[str] = typer.Option(
        None, "--unit", help="systemd unit name, for example my-app.service."
    ),
    since: str = typer.Option("1h", help="Start of the log window, such as 1h or 30m."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the command without running it."
    ),
) -> None:
    """Follow recent logs from one Docker project or systemd unit.

    Args:
        project: Docker project slug.
        unit: systemd unit name.
        since: Start of the log window.
        dry_run: Whether to print a command plan instead of running it.

    Returns:
        None.
    """
    source_name, log_argv_list = _build_source_command(
        project=project, unit=unit, since=since, follow=True
    )
    if dry_run:
        console.print("[bold green]zata-ops logs tail dry-run[/bold green]")
        console.print_json(
            json.dumps({"source": source_name, "argv": log_argv_list}, indent=2)
        )
        return

    try:
        process_result = subprocess.run(log_argv_list, check=False)
    except FileNotFoundError as command_error:
        console.print(
            f"[red]Required command not found: {command_error.filename}[/red]"
        )
        raise typer.Exit(code=127) from command_error

    if process_result.returncode != 0:
        raise typer.Exit(code=process_result.returncode)


@app.command("search")
def search_command(
    pattern: str = typer.Argument(
        ..., help="Literal text to find in recent log output."
    ),
    project: Optional[str] = typer.Option(
        None, help="Docker project slug; reads its <project>-backend container."
    ),
    unit: Optional[str] = typer.Option(
        None, "--unit", help="systemd unit name, for example my-app.service."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the pipeline without running it."
    ),
) -> None:
    """Search the last 24 hours of logs for literal text.

    Args:
        pattern: Literal text to search for.
        project: Docker project slug.
        unit: systemd unit name.
        dry_run: Whether to print a plan instead of running it.

    Returns:
        None.
    """
    source_name, producer_argv_list = _build_source_command(
        project=project, unit=unit, since="24h", follow=False
    )
    grep_argv_list = ["grep", "-F", "--", pattern]
    if dry_run:
        console.print("[bold green]zata-ops logs search dry-run[/bold green]")
        console.print_json(
            json.dumps(
                {
                    "source": source_name,
                    "pattern": pattern,
                    "producer_argv": producer_argv_list,
                    "consumer": grep_argv_list,
                },
                indent=2,
            )
        )
        return

    try:
        producer_process = subprocess.Popen(producer_argv_list, stdout=subprocess.PIPE)
    except FileNotFoundError as command_error:
        console.print(
            f"[red]Required command not found: {command_error.filename}[/red]"
        )
        raise typer.Exit(code=127) from command_error

    if producer_process.stdout is None:
        producer_process.terminate()
        producer_process.wait()
        console.print("[red]Could not open the log stream.[/red]")
        raise typer.Exit(code=1)

    try:
        consumer_process = subprocess.Popen(
            grep_argv_list, stdin=producer_process.stdout
        )
    except FileNotFoundError as command_error:
        producer_process.stdout.close()
        producer_process.terminate()
        producer_process.wait()
        console.print(
            f"[red]Required command not found: {command_error.filename}[/red]"
        )
        raise typer.Exit(code=127) from command_error

    producer_process.stdout.close()
    consumer_return_code = consumer_process.wait()
    producer_return_code = producer_process.wait()

    if producer_return_code != 0:
        raise typer.Exit(code=producer_return_code)
    if consumer_return_code == 1:
        console.print("[dim]No matching log lines.[/dim]")
    elif consumer_return_code != 0:
        raise typer.Exit(code=consumer_return_code)
