"""Top-level Typer entry point for the ``zata-ops`` CLI.

Mounts application inspection and operational sub-applications. Run
``zata-ops --help`` after installing via ``uv tool install`` for an overview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from zata_ops import __version__
from zata_ops.app import cli as application_cli
from zata_ops.audit_archive import archive_command
from zata_ops.db import cli as db_cli
from zata_ops.deploy import cli as deploy_cli
from zata_ops.env import cli as env_cli
from zata_ops.local_cli import local_app, service_app
from zata_ops.logs import cli as logs_cli
from zata_ops.observability import cli as observability_cli
from zata_ops.remote_cli import config_app
from zata_ops.remote_inventory import ACTIVE_PROFILE
from zata_ops.tunnel import cli as tunnel_cli

app: typer.Typer = typer.Typer(
    name="zata-ops",
    pretty_exceptions_show_locals=False,
    help=(
        "Lightweight operations toolkit for AI agents. "
        "Provides inventory-driven app inspection, db backup/restore, VPS provisioning, "
        "log inspection, and a terminal dashboard."
    ),
    no_args_is_help=True,
    add_completion=False,
)

audit_app = typer.Typer(no_args_is_help=True, add_completion=False)
audit_app.command("archive")(archive_command)
app.add_typer(audit_app, name="audit", help="Deliver OpenBao audit events.")
app.add_typer(local_app)
app.add_typer(config_app, name="config", help="Publish or roll back remote inventory.")
app.add_typer(service_app, name="service", help="Query the private service registry.")

app.add_typer(
    application_cli.app, name="app", help="Inspect applications from private inventory."
)

app.add_typer(
    db_cli.app, name="db", help="Database backup, restore, list, migrate, check."
)
app.add_typer(
    deploy_cli.app,
    name="deploy",
    help="Inspect GitHub deployment events and trigger rollbacks.",
)
app.add_typer(env_cli.app, name="env", help="VPS environment provisioning and fixes.")
app.add_typer(logs_cli.app, name="logs", help="Tail and search container/system logs.")
app.add_typer(
    observability_cli.app, name="dashboard", help="Terminal status dashboard."
)
app.add_typer(
    tunnel_cli.app,
    name="tunnel",
    help=("SSH 端口转发(local 对应 ssh -L,remote 对应 ssh -R)," "前台常驻或后台守护。"),
)


def _print_version_and_exit(value: bool) -> None:
    """Eager ``--version`` callback used by Typer."""
    if value:
        typer.echo(f"zata-ops {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        is_eager=True,
        callback=_print_version_and_exit,
        help="Show the installed zata-ops version and exit.",
    ),
    profile: Optional[Path] = typer.Option(
        None, "--profile", help="Explicit OpenBao profile."
    ),
) -> None:
    """Root callback that handles ``--version``.

    Args:
        ctx: Active CLI invocation.
        profile: Optional OpenBao profile.
        version: Triggered by Typer when ``--version`` is passed.
    """
    if profile is not None and ctx.invoked_subcommand not in {
        "service",
        "app",
        "deploy",
        "config",
    }:
        typer.echo("remote_profile_unsupported_for_command", err=True)
        raise typer.Exit(2)
    profile_context = ACTIVE_PROFILE.set(profile)
    ctx.call_on_close(lambda: ACTIVE_PROFILE.reset(profile_context))


if __name__ == "__main__":  # pragma: no cover
    app()
