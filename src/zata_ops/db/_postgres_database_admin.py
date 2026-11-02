"""PostgreSQL database backup, staging and rollback primitives."""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from zata_ops.db._service_connections import (
    DatabaseServiceConnection,
    connect_postgres_service,
)


def _inspect_postgres_target(
    target_connection: DatabaseServiceConnection,
) -> tuple[bool, bool]:
    """Return whether the target exists and whether it has user objects."""
    maintenance_connection = connect_postgres_service(target_connection, "postgres")
    try:
        with maintenance_connection.cursor() as maintenance_cursor:
            maintenance_cursor.execute(
                "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
                (target_connection.database,),
            )
            target_exists = maintenance_cursor.fetchone() is not None
    finally:
        maintenance_connection.close()
    if not target_exists:
        return False, True

    target_database_connection = connect_postgres_service(
        target_connection,
        target_connection.database,
    )
    try:
        with target_database_connection.cursor() as target_cursor:
            target_cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM pg_catalog.pg_namespace
                        WHERE nspname NOT IN ('public', 'information_schema')
                          AND nspname !~ '^pg_'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_class AS relation
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname !~ '^pg_'
                          AND namespace.nspname <> 'information_schema'
                          AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c')
                    )
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_extension
                        WHERE extname <> 'plpgsql'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_proc AS routine
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = routine.pronamespace
                        WHERE namespace.nspname = 'public'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_type AS user_type
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = user_type.typnamespace
                        WHERE namespace.nspname = 'public'
                    )
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger)
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_db_role_setting AS setting
                        JOIN pg_catalog.pg_database AS database
                          ON database.oid = setting.setdatabase
                        WHERE database.datname = current_database()
                    )
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_database
                        WHERE datname = current_database() AND datacl IS NOT NULL
                    )
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata)
                """
            )
            target_state_record = target_cursor.fetchone()
    finally:
        target_database_connection.close()
    return True, not bool(target_state_record and target_state_record[0])


def _backup_postgres_target(
    target_connection: DatabaseServiceConnection,
    backup_directory: Path,
) -> Path:
    """Create and inspect a private custom-format backup of the target database."""
    backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_directory.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / (
        f"{target_connection.database}-pre-migration-{timestamp}-{uuid.uuid4().hex[:8]}.dump"
    )
    environment = os.environ.copy()
    environment["PGPASSWORD"] = target_connection.password
    dump_command = [
        "pg_dump",
        "--format=custom",
        "--host",
        target_connection.host,
        "--port",
        str(target_connection.port),
        "--username",
        target_connection.username,
        "--dbname",
        target_connection.database,
    ]
    try:
        with backup_path.open("xb") as backup_file:
            backup_path.chmod(0o600)
            dump_result = subprocess.run(
                dump_command,
                env=environment,
                stdout=backup_file,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
        if dump_result.returncode != 0:
            raise RuntimeError(
                "Target backup failed; target database was left unchanged. "
                f"pg_dump exit={dump_result.returncode}."
            )
        archive_check = subprocess.run(
            ["pg_restore", "--list", str(backup_path)],
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
        if archive_check.returncode != 0:
            raise RuntimeError("Target backup could not be read by pg_restore.")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _create_postgres_stage_database(
    target_connection: DatabaseServiceConnection,
    stage_database_name: str,
) -> None:
    """Create an isolated target database owned by the administrative role."""
    maintenance_connection = connect_postgres_service(
        target_connection,
        "postgres",
        autocommit=True,
    )
    try:
        with maintenance_connection.cursor() as maintenance_cursor:
            maintenance_cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                    sql.Identifier(stage_database_name),
                    sql.Identifier(target_connection.username),
                )
            )
    except psycopg.Error as stage_error:
        raise RuntimeError(
            "Could not create a temporary PostgreSQL database. The target role "
            "needs CREATEDB or equivalent administrative rights."
        ) from stage_error
    finally:
        maintenance_connection.close()


def _swap_postgres_database_names(
    *,
    target_connection: DatabaseServiceConnection,
    stage_database_name: str,
    rollback_database_name: str,
    target_exists: bool,
) -> bool:
    """Promote the validated stage and keep the old database for rollback."""
    maintenance_connection = connect_postgres_service(
        target_connection,
        "postgres",
        autocommit=True,
    )
    old_database_renamed = False
    try:
        with maintenance_connection.cursor() as maintenance_cursor:
            if target_exists:
                maintenance_cursor.execute(
                    sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                        sql.Identifier(target_connection.database)
                    )
                )
                maintenance_cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (target_connection.database,),
                )
                maintenance_cursor.execute(
                    sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(target_connection.database),
                        sql.Identifier(rollback_database_name),
                    )
                )
                maintenance_cursor.execute(
                    sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                        sql.Identifier(rollback_database_name)
                    )
                )
                old_database_renamed = True
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(stage_database_name),
                    sql.Identifier(target_connection.database),
                )
            )
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                    sql.Identifier(target_connection.database)
                )
            )
    except psycopg.Error as swap_error:
        try:
            _recover_failed_database_swap(
                maintenance_connection,
                target_database_name=target_connection.database,
                rollback_database_name=rollback_database_name,
                prior_target_existed=target_exists,
            )
        except psycopg.Error as recovery_error:
            raise RuntimeError(
                "Promotion failed and automatic database-name recovery also "
                "failed. Inspect target, stage and rollback database names."
            ) from recovery_error
        raise RuntimeError(
            "Could not promote the validated PostgreSQL stage; attempted to "
            "restore the prior database name."
        ) from swap_error
    finally:
        maintenance_connection.close()
    return old_database_renamed


def _rollback_postgres_database_swap(
    *,
    target_connection: DatabaseServiceConnection,
    rollback_database_name: str,
) -> None:
    """Restore the prior database after final target verification fails."""
    maintenance_connection = connect_postgres_service(
        target_connection,
        "postgres",
        autocommit=True,
    )
    try:
        _recover_failed_database_swap(
            maintenance_connection,
            target_database_name=target_connection.database,
            rollback_database_name=rollback_database_name,
            prior_target_existed=True,
        )
    finally:
        maintenance_connection.close()


def _recover_failed_database_swap(
    maintenance_connection: psycopg.Connection,
    *,
    target_database_name: str,
    rollback_database_name: str | None,
    prior_target_existed: bool,
) -> None:
    """Restore the old target, or remove an uncommitted promoted database."""
    with maintenance_connection.cursor() as maintenance_cursor:
        maintenance_cursor.execute(
            "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
            (target_database_name,),
        )
        target_exists = maintenance_cursor.fetchone() is not None
        rollback_exists = False
        if rollback_database_name is not None:
            maintenance_cursor.execute(
                "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
                (rollback_database_name,),
            )
            rollback_exists = maintenance_cursor.fetchone() is not None
        if target_exists and (rollback_exists or not prior_target_existed):
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                    sql.Identifier(target_database_name)
                )
            )
            maintenance_cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (target_database_name,),
            )
            if rollback_exists:
                failed_database_name = _build_database_name("zata_failed")
                maintenance_cursor.execute(
                    sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(target_database_name),
                        sql.Identifier(failed_database_name),
                    )
                )
                maintenance_cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(failed_database_name)
                    )
                )
            else:
                maintenance_cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(target_database_name)
                    )
                )
            target_exists = False
        if rollback_exists and not target_exists:
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                    sql.Identifier(rollback_database_name)
                )
            )
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(rollback_database_name),
                    sql.Identifier(target_database_name),
                )
            )
        elif target_exists:
            maintenance_cursor.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                    sql.Identifier(target_database_name)
                )
            )


def _drop_postgres_database(
    target_connection: DatabaseServiceConnection,
    database_name: str,
) -> None:
    """Drop an unpromoted stage database after a failed migration."""
    maintenance_connection = connect_postgres_service(
        target_connection,
        "postgres",
        autocommit=True,
    )
    try:
        with maintenance_connection.cursor() as maintenance_cursor:
            maintenance_cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            maintenance_cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )
    finally:
        maintenance_connection.close()


def _connection_for_database(
    target_connection: DatabaseServiceConnection,
    database_name: str,
) -> str:
    """Build a libpq connection string for a named database."""
    return make_conninfo(
        host=target_connection.host,
        port=target_connection.port,
        user=target_connection.username,
        password=target_connection.password,
        dbname=database_name,
        connect_timeout=10,
    )


def _build_database_name(prefix: str) -> str:
    """Return a short, unique PostgreSQL identifier for a stage or backup DB."""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"[:63]
