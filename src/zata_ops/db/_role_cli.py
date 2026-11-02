"""Create and rotate restricted PostgreSQL application roles."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

import psycopg
import typer
from psycopg import sql
from rich.console import Console

from zata_ops.db._service_connections import (
    DatabaseServiceConnection,
    connect_postgres_service,
    resolve_database_service,
    resolve_service_record,
)
from zata_ops.private_credentials import (
    credential_status,
    remove_secret,
    store_secret,
    validate_private_directory,
)
from zata_ops.services import (
    ServiceConfig,
    ServicesConfig,
    load_services,
    write_services_config,
)

console = Console()
_ROLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
_SEQUENCE_PRIVILEGES = "USAGE, SELECT, UPDATE"
_MINIMUM_ROLE_ADMIN_SERVER_VERSION = 110_000


def register_role_commands(database_app: typer.Typer) -> None:
    """Add the PostgreSQL role command group to the database CLI."""
    role_app = typer.Typer(no_args_is_help=True, add_completion=False)
    database_app.add_typer(role_app, name="role")

    @role_app.command("create")
    def create_role_command(
        admin_service: str = typer.Option(
            ..., "--admin-service", help="Registered PostgreSQL administrator service."
        ),
        role_name: str = typer.Option(..., "--role", help="New login role name."),
        service_name: str = typer.Option(
            ..., "--service-name", help="Name for the new private service record."
        ),
        database_name: str | None = typer.Option(
            None, "--database", help="Database receiving application privileges."
        ),
        notes: str = typer.Option(
            "", "--notes", help="Non-secret note saved with the service record."
        ),
        private_dir: Path = typer.Option(
            Path("config/private"), "--private-dir", help="Private config directory."
        ),
    ) -> None:
        """Create a least-privilege application role and save its password."""
        try:
            validate_private_directory(private_dir)
            admin_connection = resolve_database_service(
                private_dir=private_dir,
                service_name=admin_service,
                expected_engine="postgresql",
            )
            target_database_name = database_name or admin_connection.database
            generated_password = secrets.token_urlsafe(48)
            new_service = _build_role_service(
                private_dir=private_dir,
                admin_connection=admin_connection,
                role_name=role_name,
                service_name=service_name,
                database_name=target_database_name,
                notes=notes,
            )
            _create_postgres_role(
                admin_connection=admin_connection,
                database_name=target_database_name,
                role_name=role_name,
                password=generated_password,
            )
            try:
                store_secret(
                    private_dir / "secrets.env",
                    new_service.password_env,
                    generated_password,
                )
                existing_services = load_services(private_dir / "services.toml")
                write_services_config(
                    private_dir / "services.toml",
                    ServicesConfig(services=[*existing_services.services, new_service]),
                )
            except (OSError, ValueError):
                _drop_postgres_role(
                    admin_connection=admin_connection,
                    database_name=target_database_name,
                    role_name=role_name,
                )
                try:
                    remove_secret(private_dir / "secrets.env", new_service.password_env)
                except (OSError, ValueError) as cleanup_error:
                    raise RuntimeError(
                        "Role setup was removed from PostgreSQL, but its unused "
                        "local credential could not be removed. Check the private "
                        "credential reference and backups."
                    ) from cleanup_error
                raise
            console.print(
                f"[green]Role {role_name!r} created for database "
                f"{target_database_name!r}; service {service_name!r} registered. "
                "The generated password was saved privately and is not displayed.[/green]"
            )
        except (OSError, RuntimeError, ValueError, psycopg.Error) as role_error:
            console.print(
                f"[red]Role creation failed: {_safe_role_error(role_error)}[/red]",
                stderr=True,
            )
            raise typer.Exit(code=1) from role_error

    @role_app.command("rotate")
    def rotate_role_command(
        admin_service: str = typer.Option(
            ..., "--admin-service", help="Registered PostgreSQL administrator service."
        ),
        service_name: str = typer.Option(
            ..., "--service-name", help="Registered application service to rotate."
        ),
        private_dir: Path = typer.Option(
            Path("config/private"), "--private-dir", help="Private config directory."
        ),
    ) -> None:
        """Rotate a registered role password and atomically save the new value."""
        try:
            validate_private_directory(private_dir)
            admin_connection = resolve_database_service(
                private_dir=private_dir,
                service_name=admin_service,
                expected_engine="postgresql",
            )
            role_service = resolve_service_record(
                private_dir=private_dir,
                service_name=service_name,
                expected_engine="postgresql",
            )
            previous_password = os.environ.get(role_service.password_env)
            target_database_name = role_service.database
            _require_same_postgres_endpoint(admin_connection, role_service)
            generated_password = secrets.token_urlsafe(48)
            store_secret(
                private_dir / "secrets.env",
                role_service.password_env,
                generated_password,
            )
            try:
                _alter_postgres_role_password(
                    admin_connection=admin_connection,
                    database_name=target_database_name,
                    role_name=role_service.username,
                    password=generated_password,
                )
            except (psycopg.Error, RuntimeError):
                if _role_password_is_accepted(
                    admin_connection,
                    database_name=target_database_name,
                    role_name=role_service.username,
                    password=generated_password,
                ):
                    pass
                elif previous_password and _role_password_is_accepted(
                    admin_connection,
                    database_name=target_database_name,
                    role_name=role_service.username,
                    password=previous_password,
                ):
                    store_secret(
                        private_dir / "secrets.env",
                        role_service.password_env,
                        previous_password,
                    )
                    raise RuntimeError(
                        "Password rotation failed; the previous database password "
                        "was confirmed and restored locally."
                    ) from None
                else:
                    raise RuntimeError(
                        "Password rotation could not be confirmed. The new value "
                        "remains saved locally; verify the database role before "
                        "restarting the application."
                    ) from None
            console.print(
                f"[green]Password for service {service_name!r} rotated and saved "
                "privately. Restart or reload the application to use it.[/green]"
            )
        except (OSError, RuntimeError, ValueError, psycopg.Error) as role_error:
            console.print(
                f"[red]Role rotation failed: {_safe_role_error(role_error)}[/red]",
                stderr=True,
            )
            raise typer.Exit(code=1) from role_error


def _build_role_service(
    *,
    private_dir: Path,
    admin_connection: DatabaseServiceConnection,
    role_name: str,
    service_name: str,
    database_name: str,
    notes: str,
) -> ServiceConfig:
    """Validate a new role and its corresponding private service metadata."""
    if not _ROLE_NAME.fullmatch(role_name):
        raise ValueError("Role names must use ASCII letters, digits and underscores.")
    if role_name == admin_connection.username:
        raise ValueError("The application role must differ from the admin role.")
    if not database_name:
        raise ValueError("Database name cannot be empty.")
    services_config = load_services(private_dir / "services.toml")
    if any(service.name == service_name for service in services_config.services):
        raise ValueError(f"Service {service_name!r} is already registered.")
    credential_reference = (
        "SERVICE_"
        + hashlib.sha256(service_name.encode("utf-8")).hexdigest()[:16].upper()
        + "_PASSWORD"
    )
    if credential_reference in credential_status(private_dir / "secrets.env"):
        raise ValueError("The generated credential reference is already configured.")
    if any(
        service.password_env == credential_reference
        for service in services_config.services
    ):
        raise ValueError("The generated credential reference is already in use.")
    return ServiceConfig(
        name=service_name,
        server=next(
            service.server
            for service in services_config.services
            if service.name == admin_connection.service_name
        ),
        type="postgresql",
        host=admin_connection.host,
        port=admin_connection.port,
        database=database_name,
        username=role_name,
        url="",
        password_env=credential_reference,
        notes=notes,
    )


def _create_postgres_role(
    *,
    admin_connection: DatabaseServiceConnection,
    database_name: str,
    role_name: str,
    password: str,
) -> None:
    """Create a login role and grant application data access in one transaction."""
    try:
        with connect_postgres_service(
            admin_connection, database_name
        ) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role_name,),
                )
                if postgres_cursor.fetchone() is not None:
                    raise ValueError(f"PostgreSQL role {role_name!r} already exists.")
                postgres_cursor.execute(
                    "SELECT current_user, "
                    "COALESCE(NULLIF(current_setting('scram_iterations', true), ''), '4096'), "
                    "current_setting('server_version_num')"
                )
                admin_name, iteration_text, server_version_text = (
                    postgres_cursor.fetchone()
                )
                _require_role_admin_support(int(server_version_text))
                verifier = _scram_verifier(password, max(4096, int(iteration_text)))
                postgres_cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
                    ).format(sql.Identifier(role_name), sql.Literal(verifier))
                )
                _grant_application_privileges(
                    postgres_cursor,
                    database_name=database_name,
                    admin_name=str(admin_name),
                    role_name=role_name,
                )
                postgres_cursor.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'CREATE')",
                    (role_name,),
                )
                if postgres_cursor.fetchone()[0]:
                    raise RuntimeError(
                        "The public schema grants CREATE through inherited access. "
                        "Remove that grant in this dedicated database before creating "
                        "a DML-only application role."
                    )
    except psycopg.Error as database_error:
        raise RuntimeError(
            "PostgreSQL rejected role creation or privilege setup. Confirm the "
            "admin service has CREATEROLE and owns the target database objects."
        ) from database_error


def _grant_application_privileges(
    postgres_cursor: psycopg.Cursor,
    *,
    database_name: str,
    admin_name: str,
    role_name: str,
) -> None:
    """Grant DML, sequence use and future-object access without DDL ownership."""
    postgres_cursor.execute(
        sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(database_name), sql.Identifier(role_name)
        )
    )
    postgres_cursor.execute(
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role_name))
    )
    postgres_cursor.execute(
        sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA public TO {}").format(
            sql.SQL(_TABLE_PRIVILEGES), sql.Identifier(role_name)
        )
    )
    postgres_cursor.execute(
        sql.SQL("GRANT {} ON ALL SEQUENCES IN SCHEMA public TO {}").format(
            sql.SQL(_SEQUENCE_PRIVILEGES), sql.Identifier(role_name)
        )
    )
    postgres_cursor.execute(
        sql.SQL("GRANT EXECUTE ON ALL ROUTINES IN SCHEMA public TO {}").format(
            sql.Identifier(role_name)
        )
    )
    postgres_cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT {} ON TABLES TO {}"
        ).format(
            sql.Identifier(admin_name),
            sql.SQL(_TABLE_PRIVILEGES),
            sql.Identifier(role_name),
        )
    )
    postgres_cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT {} ON SEQUENCES TO {}"
        ).format(
            sql.Identifier(admin_name),
            sql.SQL(_SEQUENCE_PRIVILEGES),
            sql.Identifier(role_name),
        )
    )
    postgres_cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT EXECUTE ON ROUTINES TO {}"
        ).format(sql.Identifier(admin_name), sql.Identifier(role_name))
    )
    postgres_cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT USAGE ON TYPES TO {}"
        ).format(sql.Identifier(admin_name), sql.Identifier(role_name))
    )


def _alter_postgres_role_password(
    *,
    admin_connection: DatabaseServiceConnection,
    database_name: str,
    role_name: str,
    password: str,
) -> None:
    """Replace a role password using a one-way SCRAM verifier."""
    try:
        with connect_postgres_service(
            admin_connection, database_name
        ) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    "SELECT COALESCE(NULLIF(current_setting('scram_iterations', true), ''), '4096'), "
                    "current_setting('server_version_num')"
                )
                iteration_text, server_version_text = postgres_cursor.fetchone()
                _require_role_admin_support(int(server_version_text))
                verifier = _scram_verifier(password, max(4096, int(iteration_text)))
                postgres_cursor.execute(
                    sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                        sql.Identifier(role_name), sql.Literal(verifier)
                    )
                )
    except psycopg.Error as database_error:
        raise RuntimeError(
            "PostgreSQL rejected the role password change; the previous local "
            "credential was restored if it could be saved."
        ) from database_error


def _drop_postgres_role(
    *,
    admin_connection: DatabaseServiceConnection,
    database_name: str,
    role_name: str,
) -> None:
    """Remove an incompletely registered role and its target-database grants."""
    try:
        with connect_postgres_service(
            admin_connection, database_name
        ) as postgres_connection:
            with postgres_connection.cursor() as postgres_cursor:
                postgres_cursor.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name))
                )
                postgres_cursor.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                )
    except psycopg.Error as cleanup_error:
        raise RuntimeError(
            "Local role registration failed, and automatic database-role cleanup "
            "also failed. Review the role and private credential records."
        ) from cleanup_error


def _scram_verifier(password: str, iterations: int) -> str:
    """Build a PostgreSQL SCRAM-SHA-256 verifier for a generated ASCII secret."""
    if iterations < 4096 or iterations > 10_000_000:
        raise ValueError("PostgreSQL SCRAM iteration setting is outside safe bounds.")
    salt_bytes = secrets.token_bytes(16)
    salted_password = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, iterations
    )
    client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted_password, b"Server Key", hashlib.sha256).digest()
    salt_text = base64.b64encode(salt_bytes).decode("ascii")
    stored_key_text = base64.b64encode(stored_key).decode("ascii")
    server_key_text = base64.b64encode(server_key).decode("ascii")
    return (
        f"SCRAM-SHA-256${iterations}:{salt_text}"
        f"${stored_key_text}:{server_key_text}"
    )


def _require_role_admin_support(server_version_num: int) -> None:
    """Require PostgreSQL 11 syntax for supported role privilege statements."""
    if server_version_num < _MINIMUM_ROLE_ADMIN_SERVER_VERSION:
        raise RuntimeError(
            "PostgreSQL 11 or newer is required for role administration commands."
        )


def _require_same_postgres_endpoint(
    admin_connection: DatabaseServiceConnection, role_service: ServiceConfig
) -> None:
    """Ensure rotation metadata points to the same registered cluster endpoint."""
    if (admin_connection.host, admin_connection.port) != (
        role_service.host,
        role_service.port,
    ):
        raise ValueError(
            "Admin and application services must use the same host and port for rotation."
        )


def _role_password_is_accepted(
    admin_connection: DatabaseServiceConnection,
    *,
    database_name: str,
    role_name: str,
    password: str,
) -> bool:
    """Check whether a role password currently authenticates without output."""
    try:
        with connect_postgres_service(
            admin_connection,
            database_name,
            username=role_name,
            password=password,
        ) as role_connection:
            with role_connection.cursor() as role_cursor:
                role_cursor.execute("SELECT 1")
                return role_cursor.fetchone() == (1,)
    except psycopg.Error:
        return False


def _safe_role_error(error: Exception) -> str:
    """Return a non-sensitive explanation without driver query details."""
    if isinstance(error, psycopg.Error):
        return (
            "Database operation failed; connection and privilege details were hidden."
        )
    return str(error)
