"""JSON-only CLI adapter for inventory-driven application observations."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from zata_ops.app._inspect import InspectionOptions, inspect_application
from zata_ops.config import DEFAULT_PRIVATE_DIR, PRIVATE_DIR_ENV
from zata_ops.inventory import load_inventory, resolve_application
from zata_ops.openbao import OpenBaoError
from zata_ops.remote_inventory import RemoteInventory, remote_client

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("inspect")
def inspect_command(
    application_name: str = typer.Argument(
        ..., help="Exact application name in inventory."
    ),
    server: str | None = typer.Option(
        None, help="Exact server name; required for ambiguous applications."
    ),
    inventory: Path | None = typer.Option(
        None,
        help="Local inventory TOML path (relative to current directory).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Describe checks without connecting to the target."
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
    private_directory = Path(os.environ.get(PRIVATE_DIR_ENV) or DEFAULT_PRIVATE_DIR)
    inventory_path = inventory or private_directory / "servers.toml"
    try:
        client = remote_client(
            local_override=inventory is not None or PRIVATE_DIR_ENV in os.environ
        )
        snapshot = RemoteInventory.parse(client.read_inventory()) if client else None
        inventory_config = (
            snapshot.inventory
            if snapshot
            else load_inventory(inventory_path.expanduser())
        )
    except (OSError, ValueError) as error:
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "app.inspect",
                    "status": "error",
                    "reason": error.reason
                    if isinstance(error, OpenBaoError)
                    else "invalid_inventory",
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
    if snapshot is not None and application_target.server.ssh.identity_file:
        application_target.server.ssh.identity_file = str(
            Path.home() / ".ssh" / application_target.server.ssh.identity_file
        )
    report, exit_code = inspect_application(
        InspectionOptions(target=application_target, dry_run=dry_run)
    )
    report["inventory"]["updated_at"] = inventory_config.updated_at
    if snapshot is not None:
        report["inventory"]["version"] = snapshot.record.version
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    raise typer.Exit(code=exit_code)
