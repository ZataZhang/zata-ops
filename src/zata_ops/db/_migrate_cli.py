"""Typer command registration and orchestration for PostgreSQL migration."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()


@dataclass(frozen=True)
class _PostgresMigrationOptions:
    """Configuration for one PostgreSQL snapshot migration."""

    source_db_url: str
    target_db_url: str
    work_dir_path: Path
    verify_row_counts: bool
    keep_dump: bool


def _register_migrate_command(database_app: typer.Typer) -> None:
    """Register the PostgreSQL migration command on the database Typer app.

    Args:
        database_app: Existing Typer application for database commands.

    Returns:
        None.
    """

    @database_app.command("migrate")
    def migrate_command(
        source_db_url: str = typer.Option(
            ...,
            "--source",
            envvar="ZATA_OPS_MIGRATE_SOURCE_URL",
            help="Source PostgreSQL URL; can also use ZATA_OPS_MIGRATE_SOURCE_URL.",
        ),
        target_db_url: str = typer.Option(
            ...,
            "--target",
            envvar="ZATA_OPS_MIGRATE_TARGET_URL",
            help="Target PostgreSQL URL; can also use ZATA_OPS_MIGRATE_TARGET_URL.",
        ),
        work_dir: Optional[str] = typer.Option(None, help="Local scratch directory."),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print the plan and exit."
        ),
        verify_row_counts: bool = typer.Option(
            False,
            "--verify-row-counts",
            help="Compare exact row counts after restore; use only while source writes are stopped.",
        ),
        keep_dump: bool = typer.Option(
            False,
            "--keep-dump",
            help="Keep the local SQL dump instead of deleting it after the command.",
        ),
    ) -> None:
        """Migrate PostgreSQL data from one database to another.

        This is NOT an Alembic schema migration. It runs ``pg_dump`` on the
        source URL, then pipes the dump into ``psql`` on the target URL. The
        target database must be new or empty.

        Args:
            source_db_url: Source PostgreSQL URL, or its configured environment
                variable value.
            target_db_url: Target PostgreSQL URL, or its configured environment
                variable value.
            work_dir: Optional directory for the temporary SQL dump.
            dry_run: Print the redacted plan without connecting to either database.
            verify_row_counts: Compare exact persistent-table row counts after restore.
            keep_dump: Preserve the generated local SQL dump after the command.

        Returns:
            None.

        Raises:
            typer.Exit: If the migration or optional row-count verification fails.
        """
        from zata_ops.db.cli import _resolve, _settings_or_die

        project_settings = _settings_or_die()
        migration_options = _PostgresMigrationOptions(
            source_db_url=source_db_url,
            target_db_url=target_db_url,
            work_dir_path=Path(_resolve(work_dir, project_settings.work_dir)),
            verify_row_counts=verify_row_counts,
            keep_dump=keep_dump,
        )

        if dry_run:
            _print_migration_dry_run(migration_options)
            return

        _run_postgres_migration(migration_options)


def _print_migration_dry_run(migration_options: _PostgresMigrationOptions) -> None:
    """Print a redacted migration plan without connecting to either database."""
    from zata_ops.db.cli import _redact_db_url

    console.print("[bold green]zata-ops db migrate --dry-run[/bold green]")
    console.print_json(
        json.dumps(
            {
                "source_db_url": _redact_db_url(migration_options.source_db_url),
                "target_db_url": _redact_db_url(migration_options.target_db_url),
                "intermediate_dump": (
                    "temporary SQL dump under " f"{migration_options.work_dir_path}"
                ),
                "verify_row_counts": migration_options.verify_row_counts,
                "keep_dump": migration_options.keep_dump,
                "note": (
                    "Dry run does not connect to either database. The target "
                    "must be new or empty; this is data migration, not an "
                    "Alembic schema migration."
                ),
            }
        )
    )


def _run_postgres_migration(migration_options: _PostgresMigrationOptions) -> None:
    """Dump, restore, optionally verify, and clean up a PostgreSQL snapshot."""
    from zata_ops.db.cli import _redact_db_url
    from zata_ops.db._database import backup_database, restore_database

    migration_options.work_dir_path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="migrate-",
        suffix=".sql.gz",
        dir=migration_options.work_dir_path,
        delete=False,
    ) as migration_dump_file:
        migration_dump_path = Path(migration_dump_file.name)

    try:
        console.print(
            f"[cyan]Dumping[/cyan] {_redact_db_url(migration_options.source_db_url)}"
        )
        backup_database(migration_options.source_db_url, migration_dump_path)
        console.print(
            "[cyan]Restoring[/cyan] into "
            f"{_redact_db_url(migration_options.target_db_url)}"
        )
        restore_database(
            migration_options.target_db_url,
            migration_dump_path,
            require_empty_target=True,
        )

        if migration_options.verify_row_counts:
            _verify_postgres_migration_row_counts(
                source_db_url=migration_options.source_db_url,
                target_db_url=migration_options.target_db_url,
            )

        console.print("[green]Migration complete.[/green]")
        if migration_options.keep_dump:
            console.print(f"[cyan]SQL dump kept at[/cyan] {migration_dump_path}")
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as migration_error:
        console.print(
            f"[red]Migration command stopped with an error:[/red] {migration_error}",
            stderr=True,
        )
        if migration_options.keep_dump and migration_dump_path.exists():
            console.print(f"[yellow]SQL dump kept at[/yellow] {migration_dump_path}")
        raise typer.Exit(code=1) from migration_error
    finally:
        if not migration_options.keep_dump:
            try:
                migration_dump_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                console.print(
                    f"[yellow]Could not remove temporary SQL dump "
                    f"{migration_dump_path}: {cleanup_error}[/yellow]",
                    stderr=True,
                )


def _verify_postgres_migration_row_counts(
    *,
    source_db_url: str,
    target_db_url: str,
) -> None:
    """Compare exact row counts for each persistent table on source and target."""
    from zata_ops.db._postgres_migration import _get_postgres_table_row_counts

    source_table_row_counts = _get_postgres_table_row_counts(source_db_url)
    target_table_row_counts = _get_postgres_table_row_counts(target_db_url)
    if source_table_row_counts != target_table_row_counts:
        differing_table_names = sorted(
            set(source_table_row_counts) | set(target_table_row_counts)
        )
        mismatch_details = [
            (
                f"{schema_name}.{table_name}: "
                f"source={source_table_row_counts.get((schema_name, table_name), 'missing')}, "
                f"target={target_table_row_counts.get((schema_name, table_name), 'missing')}"
            )
            for schema_name, table_name in differing_table_names
            if source_table_row_counts.get((schema_name, table_name))
            != target_table_row_counts.get((schema_name, table_name))
        ]
        mismatch_preview = "\n".join(mismatch_details[:20])
        remaining_mismatch_count = len(mismatch_details) - 20
        if remaining_mismatch_count > 0:
            mismatch_preview += (
                f"\n... and {remaining_mismatch_count} more table mismatch(es)"
            )
        raise RuntimeError(
            "Migration completed, but exact table row counts differ. "
            "The source may have changed during verification.\n"
            f"{mismatch_preview}"
        )
    console.print(
        "[green]Row-count verification passed for "
        f"{len(source_table_row_counts)} table(s).[/green]"
    )
