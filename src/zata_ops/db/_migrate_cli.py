"""Typer command registration and orchestration for PostgreSQL migration."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psycopg
import pymysql
import typer
from rich.console import Console

from zata_ops.db._mysql_migration import (
    MySQLMigrationOptions,
    run_mysql_postgres_migration,
)
from zata_ops.db._service_connections import resolve_database_service
from zata_ops.private_credentials import validate_private_directory

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
        source_db_url: Optional[str] = typer.Option(
            None,
            "--source",
            envvar="ZATA_OPS_MIGRATE_SOURCE_URL",
            help="Source PostgreSQL URL for PostgreSQL-to-PostgreSQL migration.",
        ),
        target_db_url: Optional[str] = typer.Option(
            None,
            "--target",
            envvar="ZATA_OPS_MIGRATE_TARGET_URL",
            help="Target PostgreSQL URL for PostgreSQL-to-PostgreSQL migration.",
        ),
        source_engine: str = typer.Option(
            "postgresql",
            "--source-engine",
            help="Source engine: postgresql or mysql.",
        ),
        source_service: Optional[str] = typer.Option(
            None, "--source-service", help="Registered MySQL source service name."
        ),
        target_service: Optional[str] = typer.Option(
            None, "--target-service", help="Registered PostgreSQL target service name."
        ),
        replace_target: bool = typer.Option(
            False,
            "--replace-target",
            help="Back up, stage and replace a PostgreSQL service target; keep rollback DB.",
        ),
        private_dir: Path = typer.Option(
            Path("config/private"), "--private-dir", help="Private config directory."
        ),
        backup_dir: Optional[str] = typer.Option(
            None, "--backup-dir", help="Private directory for target backups."
        ),
        pgloader_binary: str = typer.Option(
            "pgloader", "--pgloader", help="pgloader executable for MySQL imports."
        ),
        timeout_seconds: int = typer.Option(
            3600,
            "--timeout-seconds",
            min=1,
            help="Maximum MySQL import duration in seconds; default is one hour.",
        ),
        work_dir: Optional[str] = typer.Option(None, help="Local scratch directory."),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print the plan and exit."
        ),
        verify_row_counts: bool = typer.Option(
            False,
            "--verify-row-counts",
            help="Compare PostgreSQL snapshot row counts; MySQL migrations always do this.",
        ),
        keep_dump: bool = typer.Option(
            False,
            "--keep-dump",
            help="Keep the PostgreSQL snapshot dump instead of deleting it after the command.",
        ),
    ) -> None:
        """Migrate a PostgreSQL snapshot or stage a MySQL-to-PostgreSQL import.

        PostgreSQL snapshots use ``pg_dump`` and ``psql`` and require an empty
        target. MySQL imports use registered service records and ``pgloader``;
        they run in a staged PostgreSQL database and require ``--replace-target``
        before replacing a non-empty target. Neither mode is an Alembic migration.

        Args:
            source_db_url: Source PostgreSQL URL, or its configured environment
                variable value for PostgreSQL snapshots.
            target_db_url: Target PostgreSQL URL, or its configured environment
                variable value for PostgreSQL snapshots.
            source_engine: Source database engine.
            source_service: Registered MySQL service for cross-engine migrations.
            target_service: Registered PostgreSQL service for cross-engine migrations.
            replace_target: Back up and swap a non-empty target after verification.
            private_dir: Private config path used for service metadata and secrets.
            backup_dir: Optional private target-backup directory.
            pgloader_binary: Executable used by the MySQL migration path.
            timeout_seconds: Maximum duration for the MySQL import stage.
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

        if source_engine == "mysql":
            if keep_dump or work_dir:
                raise typer.BadParameter(
                    "--keep-dump and --work-dir are only used by PostgreSQL snapshots."
                )
            if source_db_url or target_db_url:
                raise typer.BadParameter(
                    "Use --source-service and --target-service for MySQL migration; "
                    "database URLs are not accepted in this mode."
                )
            if not source_service or not target_service:
                raise typer.BadParameter(
                    "MySQL migration requires --source-service and --target-service."
                )
            if dry_run:
                _print_mysql_migration_dry_run(
                    source_service=source_service,
                    target_service=target_service,
                    replace_target=replace_target,
                    pgloader_binary=pgloader_binary,
                    timeout_seconds=timeout_seconds,
                    backup_dir=backup_dir,
                )
                return
            _run_mysql_migration(
                source_service=source_service,
                target_service=target_service,
                replace_target=replace_target,
                private_dir=private_dir,
                backup_dir=backup_dir,
                pgloader_binary=pgloader_binary,
                timeout_seconds=timeout_seconds,
            )
            return
        if source_engine != "postgresql":
            raise typer.BadParameter("--source-engine must be postgresql or mysql.")
        if source_service or target_service:
            raise typer.BadParameter(
                "--source-service and --target-service are only used with MySQL sources."
            )
        if backup_dir or pgloader_binary != "pgloader" or timeout_seconds != 3600:
            raise typer.BadParameter(
                "--backup-dir, --pgloader and --timeout-seconds are only used with MySQL sources."
            )
        if replace_target:
            raise typer.BadParameter(
                "--replace-target is currently supported for the staged MySQL-to-PostgreSQL path."
            )
        if not source_db_url or not target_db_url:
            raise typer.BadParameter(
                "PostgreSQL migration requires --source and --target, or their environment variables."
            )
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


def _print_mysql_migration_dry_run(
    *,
    source_service: str,
    target_service: str,
    replace_target: bool,
    pgloader_binary: str,
    timeout_seconds: int,
    backup_dir: str | None,
) -> None:
    """Print the registered-service migration plan without connecting."""
    console.print("[bold green]zata-ops db migrate --dry-run[/bold green]")
    console.print_json(
        json.dumps(
            {
                "source_engine": "mysql",
                "source_service": source_service,
                "target_engine": "postgresql",
                "target_service": target_service,
                "replace_target": replace_target,
                "target_backup_directory": backup_dir
                or "config/private/backups/db-migration",
                "pgloader": pgloader_binary,
                "timeout_seconds": timeout_seconds,
                "verification": [
                    "exact per-table row counts",
                    "primary/foreign/CHECK constraint counts",
                    "target backup archive readability",
                ],
                "note": "Dry run reads no credentials and makes no database connection.",
            }
        )
    )


def _run_mysql_migration(
    *,
    source_service: str,
    target_service: str,
    replace_target: bool,
    private_dir: Path,
    backup_dir: str | None,
    pgloader_binary: str,
    timeout_seconds: int,
) -> None:
    """Resolve injected service credentials and run the staged migration."""
    try:
        validate_private_directory(private_dir)
        source_connection = resolve_database_service(
            private_dir=private_dir,
            service_name=source_service,
            expected_engine="mysql",
        )
        target_connection = resolve_database_service(
            private_dir=private_dir,
            service_name=target_service,
            expected_engine="postgresql",
        )
        backup_directory = (
            Path(backup_dir) if backup_dir else private_dir / "backups" / "db-migration"
        )
        private_root = private_dir.resolve()
        private_backup_root = private_root / "backups"
        if private_backup_root.is_symlink():
            raise ValueError("Private backup root must not be a symlink.")
        resolved_backup_directory = backup_directory.resolve()
        if not resolved_backup_directory.is_relative_to(private_backup_root):
            raise ValueError(
                "Backup directory must remain inside config/private/backups."
            )
        migration_result = run_mysql_postgres_migration(
            MySQLMigrationOptions(
                source_connection=source_connection,
                target_connection=target_connection,
                backup_directory=resolved_backup_directory,
                replace_target=replace_target,
                pgloader_binary=pgloader_binary,
                timeout_seconds=timeout_seconds,
            )
        )
    except (OSError, RuntimeError, ValueError) as migration_error:
        console.print(
            f"[red]MySQL migration stopped: {migration_error}[/red]", stderr=True
        )
        raise typer.Exit(code=1) from migration_error
    except (psycopg.Error, pymysql.MySQLError) as database_error:
        console.print(
            "[red]MySQL migration stopped because a database operation failed; "
            "connection details were hidden.[/red]",
            stderr=True,
        )
        raise typer.Exit(code=1) from database_error

    console.print(
        f"[green]Migration complete: {migration_result.table_count} tables, "
        f"{migration_result.row_count} rows, "
        f"{migration_result.foreign_key_count} foreign keys, "
        f"{migration_result.check_constraint_count} CHECK constraints, "
        f"{migration_result.index_count} indexes, "
        f"{migration_result.sequence_count} sequences.[/green]"
    )
    if migration_result.target_backup_path is not None:
        console.print(f"Target backup: {migration_result.target_backup_path}")
    if migration_result.rollback_database_name is not None:
        console.print(
            f"Rollback database retained: {migration_result.rollback_database_name}"
        )


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
