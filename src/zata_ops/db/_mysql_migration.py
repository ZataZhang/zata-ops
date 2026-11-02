"""Staged MySQL-to-PostgreSQL migration with backup and database-name swap."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pymysql
from psycopg import sql
from pymysql.cursors import DictCursor

from zata_ops.db._postgres_database_admin import (
    _backup_postgres_target,
    _build_database_name,
    _connection_for_database,
    _create_postgres_stage_database,
    _drop_postgres_database,
    _inspect_postgres_target,
    _rollback_postgres_database_swap,
    _swap_postgres_database_names,
)
from zata_ops.db._service_connections import DatabaseServiceConnection


@dataclass(frozen=True)
class MySQLMigrationOptions:
    """Connection and safety settings for one MySQL-to-PostgreSQL migration."""

    source_connection: DatabaseServiceConnection
    target_connection: DatabaseServiceConnection
    backup_directory: Path
    replace_target: bool
    pgloader_binary: str = "pgloader"
    timeout_seconds: int = 3600


@dataclass(frozen=True)
class MySQLMigrationResult:
    """Durable artifacts and verification counts from a completed migration."""

    target_backup_path: Path | None
    rollback_database_name: str | None
    table_count: int
    row_count: int
    foreign_key_count: int
    check_constraint_count: int
    index_count: int
    sequence_count: int


@dataclass(frozen=True)
class _MySQLSourceMetadata:
    """Source object counts and compatibility metadata gathered before import."""

    table_names: tuple[str, ...]
    check_constraints: tuple[tuple[str, str, str], ...]
    current_timestamp_defaults: tuple[tuple[str, str], ...]
    primary_key_count: int
    foreign_key_count: int
    index_count: int
    auto_increment_count: int


def run_mysql_postgres_migration(
    migration_options: MySQLMigrationOptions,
) -> MySQLMigrationResult:
    """Stage, verify, back up and replace a PostgreSQL target database.

    Args:
        migration_options: Source, target and safety settings.

    Returns:
        Target backup and rollback details plus verified object counts.

    Raises:
        RuntimeError: If preflight, import, validation, backup or swap fails.
        OSError: If local backup or migration files cannot be written.
        ValueError: If an endpoint or converted MySQL check expression is invalid.
    """
    source_connection = migration_options.source_connection
    target_connection = migration_options.target_connection
    if source_connection.engine != "mysql" or target_connection.engine != "postgresql":
        raise ValueError(
            "This migration path requires MySQL source and PostgreSQL target."
        )
    if target_connection.database.lower() in {"postgres", "template0", "template1"}:
        raise ValueError(
            "A PostgreSQL maintenance or template database cannot be replaced."
        )
    if not shutil.which(migration_options.pgloader_binary):
        raise RuntimeError(
            f"pgloader executable {migration_options.pgloader_binary!r} was not found."
        )
    if migration_options.timeout_seconds <= 0:
        raise ValueError("Migration timeout must be a positive number of seconds.")

    migration_options.backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    migration_options.backup_directory.chmod(0o700)
    source_metadata = _read_mysql_source_metadata(source_connection)
    stage_database_name = _build_database_name("zata_stage")
    rollback_database_name = _build_database_name("zata_rollback")
    target_exists, target_is_empty = _inspect_postgres_target(target_connection)
    if target_exists and not target_is_empty and not migration_options.replace_target:
        raise RuntimeError(
            "Target database contains user objects. Re-run with --replace-target "
            "to create a verified stage, save a backup, and replace it."
        )

    backup_path = None
    if target_exists:
        backup_path = _backup_postgres_target(
            target_connection, migration_options.backup_directory
        )

    stage_created = False
    rollback_database_created = False
    try:
        _create_postgres_stage_database(target_connection, stage_database_name)
        stage_created = True
        pgloader_output = _run_pgloader(
            migration_options,
            stage_database_name,
        )
        _validate_pgloader_log(pgloader_output, source_metadata)

        stage_connection = _connection_for_database(
            target_connection, stage_database_name
        )
        _repair_mysql_checks_and_defaults(
            stage_connection,
            source_metadata,
        )
        source_row_counts = _read_mysql_row_counts(
            source_connection,
            source_metadata.table_names,
        )
        stage_row_counts = _read_postgres_row_counts(stage_connection)
        _require_matching_row_counts(source_row_counts, stage_row_counts)
        _verify_postgres_schema_objects(
            stage_connection,
            source_metadata,
        )
        stage_sequence_count = _read_postgres_sequence_count(stage_connection)
        if stage_sequence_count != source_metadata.auto_increment_count:
            raise RuntimeError(
                "PostgreSQL sequence count differs from MySQL AUTO_INCREMENT "
                "columns; staged database was not promoted."
            )

        rollback_database_created = _swap_postgres_database_names(
            target_connection=target_connection,
            stage_database_name=stage_database_name,
            rollback_database_name=rollback_database_name,
            target_exists=target_exists,
        )
        stage_created = False
        final_connection = _connection_for_database(
            target_connection,
            target_connection.database,
        )
        final_row_counts = _read_postgres_row_counts(final_connection)
        _require_matching_row_counts(source_row_counts, final_row_counts)

        return MySQLMigrationResult(
            target_backup_path=backup_path,
            rollback_database_name=(
                rollback_database_name if rollback_database_created else None
            ),
            table_count=len(source_row_counts),
            row_count=sum(source_row_counts.values()),
            foreign_key_count=source_metadata.foreign_key_count,
            check_constraint_count=len(source_metadata.check_constraints),
            index_count=source_metadata.index_count,
            sequence_count=stage_sequence_count,
        )
    except Exception:
        if rollback_database_created:
            _rollback_postgres_database_swap(
                target_connection=target_connection,
                rollback_database_name=rollback_database_name,
            )
        elif not target_exists and not stage_created:
            _drop_postgres_database(target_connection, target_connection.database)
        raise
    finally:
        if stage_created:
            _drop_postgres_database(target_connection, stage_database_name)


def _read_mysql_source_metadata(
    source_connection: DatabaseServiceConnection,
) -> _MySQLSourceMetadata:
    """Read table, constraint and default metadata without copying row data."""
    try:
        with pymysql.connect(
            host=source_connection.host,
            port=source_connection.port,
            user=source_connection.username,
            password=source_connection.password,
            database=source_connection.database,
            charset="utf8mb4",
            connect_timeout=10,
            cursorclass=DictCursor,
        ) as mysql_connection:
            with mysql_connection.cursor() as mysql_cursor:
                mysql_cursor.execute(
                    """
                    SELECT TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_NAME
                    """,
                    (source_connection.database,),
                )
                source_table_names = tuple(
                    str(row_record["TABLE_NAME"])
                    for row_record in mysql_cursor.fetchall()
                )
                folded_table_names = [
                    table_name.lower() for table_name in source_table_names
                ]
                if len(folded_table_names) != len(set(folded_table_names)):
                    raise RuntimeError(
                        "MySQL table names collide after PostgreSQL identifier downcasing."
                    )
                mysql_cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM information_schema.TABLES
                         WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW') AS view_count,
                        (SELECT COUNT(*) FROM information_schema.TRIGGERS
                         WHERE TRIGGER_SCHEMA = %s) AS trigger_count,
                        (SELECT COUNT(*) FROM information_schema.ROUTINES
                         WHERE ROUTINE_SCHEMA = %s) AS routine_count,
                        (SELECT COUNT(*) FROM information_schema.EVENTS
                         WHERE EVENT_SCHEMA = %s) AS event_count
                    """,
                    (source_connection.database,) * 4,
                )
                unsupported_object_counts = mysql_cursor.fetchone()
                if any(
                    int(object_count)
                    for object_count in unsupported_object_counts.values()
                ):
                    raise RuntimeError(
                        "MySQL source has views, user triggers, routines or events "
                        "that pgloader does not migrate; migrate those separately."
                    )

                mysql_cursor.execute(
                    """
                    SELECT table_constraints.TABLE_NAME, check_constraints.CONSTRAINT_NAME,
                           check_constraints.CHECK_CLAUSE
                    FROM information_schema.CHECK_CONSTRAINTS AS check_constraints
                    JOIN information_schema.TABLE_CONSTRAINTS AS table_constraints
                      USING (CONSTRAINT_SCHEMA, CONSTRAINT_NAME)
                    WHERE table_constraints.CONSTRAINT_SCHEMA = %s
                      AND table_constraints.CONSTRAINT_TYPE = 'CHECK'
                    ORDER BY table_constraints.TABLE_NAME, check_constraints.CONSTRAINT_NAME
                    """,
                    (source_connection.database,),
                )
                check_constraints = tuple(
                    (
                        str(row_record["TABLE_NAME"]),
                        str(row_record["CONSTRAINT_NAME"]),
                        str(row_record["CHECK_CLAUSE"]),
                    )
                    for row_record in mysql_cursor.fetchall()
                )
                mysql_cursor.execute(
                    """
                    SELECT COUNT(*) AS unenforced_check_count
                    FROM information_schema.TABLE_CONSTRAINTS
                    WHERE CONSTRAINT_SCHEMA = %s
                      AND CONSTRAINT_TYPE = 'CHECK'
                      AND ENFORCED = 'NO'
                    """,
                    (source_connection.database,),
                )
                unenforced_check_count = int(
                    mysql_cursor.fetchone()["unenforced_check_count"]
                )
                if unenforced_check_count:
                    raise RuntimeError(
                        "Source contains unenforced CHECK constraints; the "
                        "migration will not strengthen them silently."
                    )

                mysql_cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND LOWER(COLUMN_DEFAULT) IN
                          ('now()', 'current_timestamp', 'current_timestamp()')
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """,
                    (source_connection.database,),
                )
                current_timestamp_defaults = tuple(
                    (
                        str(row_record["TABLE_NAME"]),
                        str(row_record["COLUMN_NAME"]),
                    )
                    for row_record in mysql_cursor.fetchall()
                )
                mysql_cursor.execute(
                    """
                    SELECT CONSTRAINT_TYPE, COUNT(*) AS constraint_count
                    FROM information_schema.TABLE_CONSTRAINTS
                    WHERE CONSTRAINT_SCHEMA = %s
                      AND CONSTRAINT_TYPE IN ('PRIMARY KEY', 'FOREIGN KEY')
                    GROUP BY CONSTRAINT_TYPE
                    """,
                    (source_connection.database,),
                )
                source_constraint_counts = {
                    str(row_record["CONSTRAINT_TYPE"]): int(
                        row_record["constraint_count"]
                    )
                    for row_record in mysql_cursor.fetchall()
                }
                mysql_cursor.execute(
                    """
                    SELECT COUNT(*) AS index_count
                    FROM (
                        SELECT TABLE_NAME, INDEX_NAME
                        FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = %s
                        GROUP BY TABLE_NAME, INDEX_NAME
                    ) AS source_indexes
                    """,
                    (source_connection.database,),
                )
                source_index_count = int(mysql_cursor.fetchone()["index_count"])
                mysql_cursor.execute(
                    """
                    SELECT COUNT(*) AS auto_increment_count
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND LOWER(EXTRA) LIKE '%auto_increment%'
                    """,
                    (source_connection.database,),
                )
                source_auto_increment_count = int(
                    mysql_cursor.fetchone()["auto_increment_count"]
                )
    except pymysql.MySQLError as mysql_query_error:
        raise RuntimeError(
            "Could not inspect the MySQL source; verify its service type, host, "
            "port, database, network path and injected credential."
        ) from mysql_query_error

    if not source_table_names:
        raise RuntimeError("MySQL source contains no base tables; migration stopped.")
    return _MySQLSourceMetadata(
        table_names=source_table_names,
        check_constraints=check_constraints,
        current_timestamp_defaults=current_timestamp_defaults,
        primary_key_count=source_constraint_counts.get("PRIMARY KEY", 0),
        foreign_key_count=source_constraint_counts.get("FOREIGN KEY", 0),
        index_count=source_index_count,
        auto_increment_count=source_auto_increment_count,
    )


def _read_mysql_row_counts(
    source_connection: DatabaseServiceConnection,
    source_table_names: tuple[str, ...],
) -> dict[str, int]:
    """Return exact source table counts using escaped metadata identifiers."""
    source_row_counts: dict[str, int] = {}
    try:
        with pymysql.connect(
            host=source_connection.host,
            port=source_connection.port,
            user=source_connection.username,
            password=source_connection.password,
            database=source_connection.database,
            charset="utf8mb4",
            connect_timeout=10,
            cursorclass=DictCursor,
        ) as mysql_connection:
            with mysql_connection.cursor() as mysql_cursor:
                for source_table_name in source_table_names:
                    quoted_source_table = source_table_name.replace("`", "``")
                    mysql_cursor.execute(
                        f"SELECT COUNT(*) AS row_count FROM `{quoted_source_table}`"
                    )
                    count_record = mysql_cursor.fetchone()
                    if count_record is None:
                        raise RuntimeError(
                            f"Could not count MySQL table {source_table_name!r}."
                        )
                    source_row_counts[source_table_name.lower()] = int(
                        count_record["row_count"]
                    )
    except pymysql.MySQLError as mysql_count_error:
        raise RuntimeError(
            "Could not read exact MySQL row counts. Stop source writes or retry "
            "after verifying the connection."
        ) from mysql_count_error
    return source_row_counts


def _run_pgloader(
    migration_options: MySQLMigrationOptions,
    stage_database_name: str,
) -> str:
    """Run pgloader with a credential-free load file and injected environment."""
    source_connection = migration_options.source_connection
    target_connection = migration_options.target_connection
    load_text = (
        "LOAD DATABASE\n"
        f"     FROM mysql://{_quote_uri_part(source_connection.username)}@"
        f"{_format_url_host(source_connection.host)}:{source_connection.port}/"
        f"{_quote_uri_part(source_connection.database)}\n"
        f"     INTO postgresql://{_quote_uri_part(target_connection.username)}@"
        f"{_format_url_host(target_connection.host)}:{target_connection.port}/"
        f"{_quote_uri_part(stage_database_name)}\n"
        "WITH include drop, create tables, create indexes, reset sequences, foreign keys,\n"
        "     on error stop\n"
        f"ALTER SCHEMA '{_quote_pgloader_schema(source_connection.database)}' "
        "RENAME TO 'public';\n"
    )
    load_path: Path | None = None
    pgpass_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="mysql-pg-",
            suffix=".load",
            dir=migration_options.backup_directory,
            delete=False,
        ) as load_file:
            load_path = Path(load_file.name)
            load_file.write(load_text)
        load_path.chmod(0o600)
        pgpass_fields = (
            target_connection.host,
            str(target_connection.port),
            stage_database_name,
            target_connection.username,
            target_connection.password,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="mysql-pg-",
            suffix=".pgpass",
            dir=migration_options.backup_directory,
            delete=False,
        ) as pgpass_file:
            pgpass_path = Path(pgpass_file.name)
            pgpass_file.write(
                ":".join(_escape_pgpass_field(field) for field in pgpass_fields)
            )
            pgpass_file.write("\n")
        pgpass_path.chmod(0o600)
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = source_connection.password
        environment["PGPASSWORD"] = target_connection.password
        environment["PGPASSFILE"] = str(pgpass_path)
        load_result = subprocess.run(
            [migration_options.pgloader_binary, str(load_path)],
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=migration_options.timeout_seconds,
        )
    except subprocess.TimeoutExpired as timeout_error:
        raise RuntimeError(
            "pgloader exceeded its configured time limit."
        ) from timeout_error
    finally:
        if load_path is not None:
            load_path.unlink(missing_ok=True)
        if pgpass_path is not None:
            pgpass_path.unlink(missing_ok=True)
    combined_output = load_result.stdout + "\n" + load_result.stderr
    if load_result.returncode != 0:
        raise RuntimeError(
            f"pgloader failed with exit={load_result.returncode}; "
            "the staged database was not promoted."
        )
    return combined_output


def _validate_pgloader_log(
    pgloader_output: str,
    source_metadata: _MySQLSourceMetadata,
) -> None:
    """Reject import errors except check DDL that the compatibility pass repairs."""
    source_check_names = {
        constraint_name.lower()[:63]
        for _, constraint_name, _ in source_metadata.check_constraints
    }
    output_lines = pgloader_output.splitlines()
    pgloader_error_lines = [
        index
        for index, output_line in enumerate(output_lines)
        if re.search(r"\bERROR\b", output_line, flags=re.IGNORECASE)
    ]
    for error_index in pgloader_error_lines:
        error_context = "\n".join(output_lines[error_index : error_index + 4])
        constraint_match = re.search(
            r"ADD\s+CONSTRAINT\s+[\"`]?([A-Za-z_][A-Za-z0-9_$]*)[\"`]?\s+CHECK\b",
            error_context,
            flags=re.IGNORECASE,
        )
        if (
            not constraint_match
            or constraint_match.group(1).lower()[:63] not in source_check_names
        ):
            raise RuntimeError(
                "pgloader emitted an error outside a source CHECK constraint; "
                "the staged database was not promoted."
            )
    unexpected_warnings = [
        output_line
        for output_line in output_lines
        if re.search(r"\bWARNING\b", output_line, flags=re.IGNORECASE)
        and 'Failed to transform default value "now()"' not in output_line
    ]
    if unexpected_warnings:
        raise RuntimeError(
            "pgloader emitted an unhandled warning; the staged database was "
            "not promoted."
        )


def _repair_mysql_checks_and_defaults(
    stage_connection_string: str,
    source_metadata: _MySQLSourceMetadata,
) -> None:
    """Restore simple MySQL CHECK expressions and NOW defaults in PostgreSQL."""
    try:
        with psycopg.connect(stage_connection_string) as stage_connection:
            with stage_connection.cursor() as stage_cursor:
                stage_cursor.execute(
                    """
                    SELECT relation.relname, constraint_record.conname
                    FROM pg_catalog.pg_constraint AS constraint_record
                    JOIN pg_catalog.pg_class AS relation
                      ON relation.oid = constraint_record.conrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND constraint_record.contype = 'c'
                    """
                )
                existing_checks = {
                    (str(table_name), str(constraint_name))
                    for table_name, constraint_name in stage_cursor.fetchall()
                }
                for (
                    table_name,
                    constraint_name,
                    raw_expression,
                ) in source_metadata.check_constraints:
                    target_table_name = table_name.lower()
                    target_constraint_name = constraint_name.lower()[:63]
                    if (target_table_name, target_constraint_name) in existing_checks:
                        continue
                    translated_expression = _translate_mysql_check_expression(
                        raw_expression
                    )
                    stage_cursor.execute(
                        sql.SQL(
                            "ALTER TABLE {}.{} ADD CONSTRAINT {} CHECK ({})"
                        ).format(
                            sql.Identifier("public"),
                            sql.Identifier(target_table_name),
                            sql.Identifier(target_constraint_name),
                            sql.SQL(translated_expression),
                        )
                    )

                for (
                    table_name,
                    column_name,
                ) in source_metadata.current_timestamp_defaults:
                    target_table_name = table_name.lower()
                    target_column_name = column_name.lower()
                    stage_cursor.execute(
                        """
                        SELECT column_default
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = %s
                          AND column_name = %s
                        """,
                        (target_table_name, target_column_name),
                    )
                    column_record = stage_cursor.fetchone()
                    if column_record is None:
                        raise RuntimeError(
                            f"MySQL default column {table_name}.{column_name} "
                            "was not created in the PostgreSQL stage."
                        )
                    if column_record[0] is None:
                        stage_cursor.execute(
                            sql.SQL(
                                "ALTER TABLE {}.{} ALTER COLUMN {} "
                                "SET DEFAULT CURRENT_TIMESTAMP"
                            ).format(
                                sql.Identifier("public"),
                                sql.Identifier(target_table_name),
                                sql.Identifier(target_column_name),
                            )
                        )
    except psycopg.Error as compatibility_error:
        raise RuntimeError(
            "Could not restore source CHECK constraints or current-timestamp "
            "defaults in the staged PostgreSQL database."
        ) from compatibility_error


def _translate_mysql_check_expression(mysql_expression: str) -> str:
    """Translate a safe subset of MySQL CHECK syntax for PostgreSQL."""
    if re.search(
        r"\b(REGEXP|RLIKE|DIV|JSON_EXTRACT|BINARY|SOUNDS\s+LIKE)\b|<=>|&&|\|\|",
        mysql_expression,
        flags=re.IGNORECASE,
    ):
        raise ValueError("Unsupported MySQL-only operator in a CHECK expression.")
    if re.search(r"!(?!=)", mysql_expression):
        raise ValueError("Unsupported MySQL negation operator in a CHECK expression.")

    def quote_mysql_string(match: re.Match[str]) -> str:
        literal_text = match.group(1).replace("\\'", "'").replace("\\\\", "\\")
        return "'" + literal_text.replace("'", "''") + "'"

    translated_expression = re.sub(
        r"_utf8mb4\\?'(.*?)\\?'",
        quote_mysql_string,
        mysql_expression,
        flags=re.IGNORECASE,
    )
    translated_expression = re.sub(
        r"`([^`]+)`",
        lambda match: '"' + match.group(1).lower().replace('"', '""') + '"',
        translated_expression,
    )
    translated_expression = re.sub(
        r"\b_utf8mb4\b", "", translated_expression, flags=re.IGNORECASE
    )
    if "`" in translated_expression or "\\" in translated_expression:
        raise ValueError("Unsupported quoting remains in a MySQL CHECK expression.")
    if not re.fullmatch(
        r"[A-Za-z_0-9\s.\"'()+*/%<>=!,|-]+", translated_expression
    ) or any(marker in translated_expression for marker in (";", "--", "/*", "*/")):
        raise ValueError("Unsupported token in a MySQL CHECK expression.")
    return translated_expression


def _read_postgres_row_counts(connection_string: str) -> dict[str, int]:
    """Read exact row counts for PostgreSQL base tables in the public schema."""
    try:
        with psycopg.connect(connection_string) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
                public_table_names = [
                    str(table_record[0]) for table_record in postgres_cursor.fetchall()
                ]
                row_counts: dict[str, int] = {}
                for public_table_name in public_table_names:
                    postgres_cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                            sql.Identifier("public"),
                            sql.Identifier(public_table_name),
                        )
                    )
                    row_counts[public_table_name] = int(postgres_cursor.fetchone()[0])
                return row_counts
    except psycopg.Error as postgres_count_error:
        raise RuntimeError(
            "Could not count rows in the PostgreSQL stage."
        ) from postgres_count_error


def _require_matching_row_counts(
    source_row_counts: dict[str, int],
    target_row_counts: dict[str, int],
) -> None:
    """Refuse promotion unless every table name and exact row count matches."""
    if source_row_counts != target_row_counts:
        differing_table_names = sorted(set(source_row_counts) | set(target_row_counts))
        mismatch_names = [
            table_name
            for table_name in differing_table_names
            if source_row_counts.get(table_name) != target_row_counts.get(table_name)
        ]
        raise RuntimeError(
            "Exact source/target row counts differ; staged database was not "
            "promoted. Mismatched table count: "
            f"{len(mismatch_names)}."
        )


def _verify_postgres_schema_objects(
    stage_connection_string: str,
    source_metadata: _MySQLSourceMetadata,
) -> None:
    """Compare primary, foreign and check constraint counts before promotion."""
    with psycopg.connect(stage_connection_string) as stage_connection:
        with stage_connection.cursor() as stage_cursor:
            stage_cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE constraint_record.contype = 'p'
                          AND constraint_record.convalidated
                    ),
                    COUNT(*) FILTER (
                        WHERE constraint_record.contype = 'f'
                          AND constraint_record.convalidated
                    ),
                    COUNT(*) FILTER (
                        WHERE constraint_record.contype = 'c'
                          AND constraint_record.convalidated
                    ),
                    (SELECT COUNT(*)
                     FROM pg_catalog.pg_index AS index_record
                     JOIN pg_catalog.pg_class AS index_relation
                       ON index_relation.oid = index_record.indexrelid
                     JOIN pg_catalog.pg_namespace AS index_namespace
                       ON index_namespace.oid = index_relation.relnamespace
                     WHERE index_namespace.nspname = 'public'
                       AND index_record.indisvalid)
                FROM pg_catalog.pg_constraint AS constraint_record
                JOIN pg_catalog.pg_class AS relation
                  ON relation.oid = constraint_record.conrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                """
            )
            target_constraint_counts = stage_cursor.fetchone()
    expected_constraint_counts = (
        source_metadata.primary_key_count,
        source_metadata.foreign_key_count,
        len(source_metadata.check_constraints),
        source_metadata.index_count,
    )
    if target_constraint_counts != expected_constraint_counts:
        raise RuntimeError(
            "Primary, foreign-key, CHECK constraint or index counts differ from the "
            "MySQL source; staged database was not promoted."
        )


def _read_postgres_sequence_count(connection_string: str) -> int:
    """Count public sequences that pgloader reset before promotion."""
    with psycopg.connect(connection_string) as postgres_connection:
        with postgres_connection.cursor() as postgres_cursor:
            postgres_cursor.execute(
                """
                SELECT COUNT(*) FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public' AND relation.relkind = 'S'
                """
            )
            return int(postgres_cursor.fetchone()[0])


def _quote_uri_part(value: str) -> str:
    """Percent-encode a user or database value for a password-free pgloader URI."""
    from urllib.parse import quote

    return quote(value, safe="")


def _format_url_host(host: str) -> str:
    """Bracket IPv6 hosts when rendering a connection URI."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _quote_pgloader_schema(schema_name: str) -> str:
    """Escape a MySQL schema name for pgloader's quoted schema clause."""
    return schema_name.replace("\\", "\\\\").replace("'", "\\'")


def _escape_pgpass_field(value: str) -> str:
    """Escape a PostgreSQL passfile field without writing it to command args."""
    return value.replace("\\", "\\\\").replace(":", "\\:")
