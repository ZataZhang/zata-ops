"""Read-only commands for inspecting local CLI configuration."""

from __future__ import annotations

import json
from pathlib import Path
from shutil import which
from typing import Annotated

import typer

from zata_ops import __version__
from zata_ops.config import DEFAULT_PRIVATE_DIR, PRIVATE_DIR_ENV
from zata_ops.inventory import load_inventory
from zata_ops.private_credentials import credential_status
from zata_ops.services import load_services, summarize_services

PrivateDirOption = Annotated[
    Path,
    typer.Option(
        "--private-dir", envvar=PRIVATE_DIR_ENV, help="Private configuration directory."
    ),
]

service_app = typer.Typer(no_args_is_help=True, add_completion=False)
local_app = typer.Typer()


def _emit_report(
    operation: str, report_fields: dict[str, object], *, exit_code: int = 0
) -> None:
    typer.echo(
        json.dumps(
            {"schema_version": 1, "operation": operation, **report_fields},
            ensure_ascii=False,
            indent=2,
        )
    )
    if exit_code:
        raise typer.Exit(code=exit_code)


def _query_service_registry(
    operation: str, private_dir: Path, name: str | None = None
) -> None:
    selected_directory = private_dir.expanduser()
    try:
        if not selected_directory.is_dir():
            raise ValueError("missing private directory")
        registry = load_services(
            selected_directory / "services.toml", enforce_permissions=False
        )
        selected_services = [
            service_entry
            for service_entry in registry.services
            if name is None or service_entry.name == name
        ]
        configured_credentials = credential_status(selected_directory / "secrets.env")
    except (OSError, ValueError):
        _emit_report(
            operation,
            {"status": "error", "reason": "invalid_service_configuration"},
            exit_code=2,
        )
        return
    if name is not None and not selected_services:
        _emit_report(
            operation, {"status": "error", "reason": "service_not_found"}, exit_code=1
        )
        return
    summaries = summarize_services(selected_services, configured_credentials)
    _emit_report(operation, {"status": "ok", "services": summaries})


@service_app.command("list")
def _list_services(private_dir: PrivateDirOption = DEFAULT_PRIVATE_DIR) -> None:
    """List registered services and their credential status."""
    _query_service_registry("service.list", private_dir)


@service_app.command("show")
def _show_service(
    name: str, private_dir: PrivateDirOption = DEFAULT_PRIVATE_DIR
) -> None:
    """Show the service matching an exact name."""
    _query_service_registry("service.show", private_dir, name)


@local_app.command("doctor")
def _doctor(private_dir: PrivateDirOption = DEFAULT_PRIVATE_DIR) -> None:
    """Check local configuration and SSH availability without connecting."""
    selected_directory = private_dir.expanduser().absolute()
    inventory_path = selected_directory / "servers.toml"
    services_path = selected_directory / "services.toml"
    checks = {"servers.toml": "ok", "services.toml": "ok", "ssh": "ok"}

    try:
        load_inventory(inventory_path)
    except FileNotFoundError:
        checks["servers.toml"] = "missing"
    except (OSError, ValueError):
        checks["servers.toml"] = "invalid"

    try:
        if not services_path.exists() and not services_path.is_symlink():
            checks["services.toml"] = "optional_missing"
        else:
            load_services(services_path, enforce_permissions=False)
    except (OSError, ValueError):
        checks["services.toml"] = "invalid"

    if which("ssh") is None:
        checks["ssh"] = "missing"

    exit_code = int(
        any(check not in {"ok", "optional_missing"} for check in checks.values())
    )
    _emit_report(
        "doctor",
        {
            "version": __version__,
            "private_dir": str(selected_directory),
            "status": "error" if exit_code else "ok",
            "checks": checks,
        },
        exit_code=exit_code,
    )
