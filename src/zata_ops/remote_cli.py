"""Remote inventory publishing and controlled credential execution commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from zata_ops.credential_execution import (
    SYSTEM_ENV,
    ExecutionOptions,
    run_with_environment,
)
from zata_ops.openbao import KVRecord, OpenBaoError
from zata_ops.remote_inventory import DOCUMENTS, RemoteInventory, require_remote_client
from zata_ops.services import SECRET_NAME

config_app = typer.Typer(no_args_is_help=True, add_completion=False)
_RESERVED_ENV = SYSTEM_ENV | {
    "HOME",
    "USERPROFILE",
    "COMSPEC",
    "PATHEXT",
    "VIRTUAL_ENV",
}


def _publish(
    snapshot: RemoteInventory, expected_version: int | None, *, apply: bool
) -> None:
    client = require_remote_client()
    try:
        current_record = client.read_inventory()
    except OpenBaoError as error:
        if error.reason != "openbao_not_found":
            raise
        current_record = KVRecord({}, 0)
    if apply and (
        expected_version is None or expected_version != current_record.version
    ):
        raise OpenBaoError("inventory_version_conflict")
    report = {
        "status": "preview",
        "environment": client.environment,
        "current_version": current_record.version,
        "source_version": snapshot.record.version or None,
        "documents": {
            name: "changed"
            if current_record.values.get(name) != snapshot.record.values[name]
            else "unchanged"
            for name in DOCUMENTS
        },
    }
    if apply:
        try:
            assert expected_version is not None
            report["published_version"] = client.publish_inventory(
                snapshot.record.values, expected_version
            )
        except OpenBaoError as error:
            if error.reason == "openbao_request_rejected":
                raise OpenBaoError("inventory_write_rejected") from error
            raise
        report["status"] = "published"
    typer.echo(json.dumps(report, ensure_ascii=False))


@config_app.command("publish")
def publish_command(
    from_dir: Annotated[Path, typer.Option("--from-dir")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    expected_version: Annotated[
        int | None, typer.Option("--expected-version", min=0)
    ] = None,
) -> None:
    """Preview an inventory update or publish it with a matching version."""
    try:
        _publish(
            RemoteInventory.from_directory(from_dir), expected_version, apply=apply
        )
    except OpenBaoError as error:
        typer.echo(error.reason, err=True)
        raise typer.Exit(2) from None


@config_app.command("rollback")
def rollback_command(
    to_version: Annotated[int, typer.Option("--to-version", min=1)],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    expected_version: Annotated[
        int | None, typer.Option("--expected-version", min=1)
    ] = None,
) -> None:
    """Preview or republish a validated prior inventory version."""
    try:
        client = require_remote_client()
        _publish(
            RemoteInventory.parse(client.read_inventory(to_version)),
            expected_version,
            apply=apply,
        )
    except OpenBaoError as error:
        typer.echo(error.reason, err=True)
        raise typer.Exit(2) from None


def run_service(
    credential: Annotated[
        list[str], typer.Option("--credential", metavar="SERVICE:ENV_NAME")
    ],
    command: Annotated[list[str], typer.Argument(metavar="COMMAND [ARGS]...")],
) -> None:
    """Read all selected credentials and run one trusted command."""
    try:
        client = require_remote_client()
        if not command or not credential:
            raise OpenBaoError("credential_bindings_required")
        bindings: dict[str, str] = {}
        for binding in credential:
            service_name, separator, variable = binding.rpartition(":")
            if (
                not separator
                or not service_name
                or not SECRET_NAME.fullmatch(variable)
                or variable in bindings
                or variable in _RESERVED_ENV
                or variable.startswith(("BAO_", "VAULT_", "LD_", "DYLD_", "PYTHON"))
            ):
                raise OpenBaoError("invalid_credential_binding")
            bindings[variable] = service_name
        snapshot = RemoteInventory.parse(client.read_inventory())
        references = {
            service.name: service.credential_ref
            for service in snapshot.services.services
        }
        if not references.keys() >= set(bindings.values()):
            raise OpenBaoError("service_not_found")
    except OpenBaoError as error:
        typer.echo(error.reason, err=True)
        raise typer.Exit(2) from None
    try:
        values = {
            reference: client.read_credential(reference)
            for reference in dict.fromkeys(
                references[name] for name in bindings.values()
            )
        }
    except OpenBaoError as error:
        typer.echo(f"credential_read_failed: {error.reason}", err=True)
        raise typer.Exit(1) from None
    executable = [sys.executable, *command[1:]] if command[0] == "python" else command
    try:
        exit_code = run_with_environment(
            {variable: values[references[name]] for variable, name in bindings.items()},
            executable,
            ExecutionOptions(inherit_environment=False),
        )
    except (OSError, ValueError):
        typer.echo("command_start_failed", err=True)
        raise typer.Exit(2) from None
    raise typer.Exit(exit_code)
