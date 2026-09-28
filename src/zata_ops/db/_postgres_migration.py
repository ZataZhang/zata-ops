"""PostgreSQL-specific migration preflight and verification helpers."""

from __future__ import annotations

import shutil
import subprocess

import psycopg
from psycopg import sql

from zata_ops.db._database import _build_psql_env, parse_postgres_url

_POSTGRES_TARGET_HAS_USER_OBJECTS_QUERY = """
SELECT CASE WHEN
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS user_namespace
        WHERE user_namespace.nspname NOT IN ('public', 'information_schema')
          AND user_namespace.nspname !~ '^pg_'
    )
    OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS user_relation
        JOIN pg_catalog.pg_namespace AS relation_namespace
          ON relation_namespace.oid = user_relation.relnamespace
        WHERE relation_namespace.nspname !~ '^pg_'
          AND relation_namespace.nspname <> 'information_schema'
          AND user_relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c')
    )
    OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS user_routine
        JOIN pg_catalog.pg_namespace AS routine_namespace
          ON routine_namespace.oid = user_routine.pronamespace
        WHERE routine_namespace.nspname = 'public'
    )
    OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_type AS user_type
        JOIN pg_catalog.pg_namespace AS type_namespace
          ON type_namespace.oid = user_type.typnamespace
        WHERE type_namespace.nspname = 'public'
    )
    OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_extension AS user_extension
        WHERE user_extension.extname <> 'plpgsql'
    )
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata)
THEN 1 ELSE 0 END
"""


def prepare_postgres_restore_target(
    target_db_url: str,
    *,
    require_empty_target: bool,
) -> None:
    """Create a missing PostgreSQL database or reject a detected non-empty one.

    Args:
        target_db_url: PostgreSQL URL for the restore target.
        require_empty_target: Refuse restore if common user objects are found.

    Raises:
        RuntimeError: If the target cannot be inspected or created, or if the
            strict empty-target check finds user objects.
    """
    parsed_postgres_url = parse_postgres_url(target_db_url)
    target_runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))
    target_database_name = str(parsed_postgres_url["database"])
    try:
        _run_postgres_query(
            parsed_postgres_url=parsed_postgres_url,
            runtime_env=target_runtime_env,
            database_name=target_database_name,
            query="SELECT 1",
        )
    except RuntimeError as target_connection_error:
        if _postgres_database_exists(parsed_postgres_url):
            raise RuntimeError(
                f"Target database {target_database_name!r} exists, but the target "
                f"role cannot connect to it: {target_connection_error}"
            ) from target_connection_error
    else:
        if not require_empty_target:
            return
        target_has_user_objects_output = _run_postgres_query(
            parsed_postgres_url=parsed_postgres_url,
            runtime_env=target_runtime_env,
            database_name=target_database_name,
            query=_POSTGRES_TARGET_HAS_USER_OBJECTS_QUERY,
        )
        if target_has_user_objects_output == "1":
            raise RuntimeError(
                f"Target database {target_database_name!r} already contains user "
                "objects. Restore was refused; choose a new empty database."
            )
        return

    target_database_create_command = [
        "createdb",
        "-w",
        "-h",
        str(parsed_postgres_url["host"]),
        "-p",
        str(parsed_postgres_url["port"]),
        "-U",
        str(parsed_postgres_url["user"]),
        target_database_name,
    ]
    if shutil.which("createdb"):
        target_database_create_result = subprocess.run(
            target_database_create_command,
            env=target_runtime_env,
            capture_output=True,
            check=False,
            text=True,
        )
    else:
        quoted_target_database_name = (
            '"' + target_database_name.replace('"', '""') + '"'
        )
        target_database_create_result = subprocess.run(
            [
                "psql",
                "-X",
                "-w",
                "-h",
                str(parsed_postgres_url["host"]),
                "-p",
                str(parsed_postgres_url["port"]),
                "-U",
                str(parsed_postgres_url["user"]),
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"CREATE DATABASE {quoted_target_database_name}",
            ],
            env=target_runtime_env,
            capture_output=True,
            check=False,
            text=True,
        )

    if target_database_create_result.returncode != 0:
        creation_error_text = (
            target_database_create_result.stderr.strip()
            or target_database_create_result.stdout.strip()
            or f"exit status {target_database_create_result.returncode}"
        )
        raise RuntimeError(
            f"Could not create target database {target_database_name!r}: "
            f"{creation_error_text}"
        )


def _postgres_database_exists(
    parsed_postgres_url: dict[str, str | int],
) -> bool:
    """Check database existence on the maintenance connection using a parameter."""
    postgres_connection_options: dict[str, str | int] = {
        "host": str(parsed_postgres_url["host"]),
        "port": int(parsed_postgres_url["port"]),
        "user": str(parsed_postgres_url["user"]),
        "dbname": "postgres",
    }
    target_password = str(parsed_postgres_url["password"])
    if target_password:
        postgres_connection_options["password"] = target_password

    try:
        with psycopg.connect(**postgres_connection_options) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
                    (str(parsed_postgres_url["database"]),),
                )
                return postgres_cursor.fetchone() is not None
    except psycopg.Error as postgres_query_error:
        raise RuntimeError(
            "Could not inspect PostgreSQL database existence through the "
            f"maintenance database: {postgres_query_error}"
        ) from postgres_query_error


def _run_postgres_query(
    *,
    parsed_postgres_url: dict[str, str | int],
    runtime_env: dict[str, str],
    database_name: str,
    query: str,
) -> str:
    """Run a read-only PostgreSQL query and return its unaligned output."""
    query_command_result = subprocess.run(
        [
            "psql",
            "-X",
            "-w",
            "-A",
            "-t",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            "-d",
            database_name,
            "-c",
            query,
        ],
        env=runtime_env,
        capture_output=True,
        check=False,
        text=True,
    )
    if query_command_result.returncode != 0:
        query_error_text = (
            query_command_result.stderr.strip()
            or query_command_result.stdout.strip()
            or f"exit status {query_command_result.returncode}"
        )
        raise RuntimeError(
            f"Could not inspect PostgreSQL database {database_name!r}: "
            f"{query_error_text}"
        )
    return query_command_result.stdout.strip()


def _get_postgres_table_row_counts(db_url: str) -> dict[tuple[str, str], int]:
    """Return exact row counts for persistent tables in non-system schemas."""
    try:
        with psycopg.connect(db_url) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    """
                    SELECT schemaname, tablename
                    FROM pg_catalog.pg_tables
                    WHERE schemaname <> 'information_schema'
                      AND schemaname !~ '^pg_'
                    ORDER BY schemaname, tablename
                    """
                )
                persistent_table_name_records = postgres_cursor.fetchall()
                table_row_counts: dict[tuple[str, str], int] = {}
                for schema_name, table_name in persistent_table_name_records:
                    postgres_cursor.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(schema_name), sql.Identifier(table_name)
                        )
                    )
                    table_count_record = postgres_cursor.fetchone()
                    if table_count_record is None:
                        raise RuntimeError(
                            f"Could not read row count for {schema_name}.{table_name}."
                        )
                    table_row_counts[(schema_name, table_name)] = int(
                        table_count_record[0]
                    )
    except psycopg.Error as postgres_query_error:
        raise RuntimeError(
            f"Could not verify PostgreSQL table row counts: {postgres_query_error}"
        ) from postgres_query_error

    return table_row_counts
