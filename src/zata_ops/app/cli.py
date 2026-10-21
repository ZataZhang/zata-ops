"""JSON-only CLI adapter for inventory-driven application observations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zata_ops.app._inspect import InspectionOptions, inspect_application
from zata_ops.inventory import load_inventory, resolve_application

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("inspect")
def inspect_command(
    application_name: str = typer.Argument(
        ..., help="Exact application name in inventory."
    ),
    server: str | None = typer.Option(
        None, help="Exact server name; required for ambiguous applications."
    ),
    inventory: Path = typer.Option(
        Path("config/private/servers.toml"),
        help="Local inventory TOML path (relative to current directory).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Describe checks without any network connection."
    ),
) -> None:
    """Inspect one deployment without changing remote services.

    Args:
        application_name: Inventory application name.
        server: Optional containing server name.
        inventory: Inventory file path.
        dry_run: Return a plan without connecting.

    Returns:
        Prints JSON; exits 0 (passed/preview), 1 (failure), 2 (configuration
        error), or 3 (incomplete). Argument syntax errors use Typer output.
    """
    try:
        inventory_config = load_inventory(inventory)
    except (OSError, ValueError):
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "app.inspect",
                    "status": "error",
                    "reason": "invalid_inventory",
                }
            )
        )
        raise typer.Exit(code=2)
    try:
        application_target = resolve_application(
            inventory_config, application_name, server
        )
    except ValueError as target_error:
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "app.inspect",
                    "status": "error",
                    "reason": str(target_error),
                }
            )
        )
        raise typer.Exit(code=2)
    report, exit_code = inspect_application(
        InspectionOptions(target=application_target, dry_run=dry_run)
    )
    report["inventory"]["updated_at"] = inventory_config.updated_at
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    raise typer.Exit(code=exit_code)
