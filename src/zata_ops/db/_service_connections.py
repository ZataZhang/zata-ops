"""Resolve database service records and injected credentials for DB commands."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from zata_ops.services import ServiceConfig, load_services

DatabaseEngine = Literal["postgresql", "mysql"]


@dataclass(frozen=True)
class DatabaseServiceConnection:
    """Database connection fields resolved from a private service record."""

    service_name: str
    engine: DatabaseEngine
    host: str
    port: int
    database: str
    username: str
    password: str
    password_env: str


def connect_postgres_service(
    service_connection: DatabaseServiceConnection,
    database_name: str,
    *,
    username: str | None = None,
    password: str | None = None,
    autocommit: bool = False,
) -> psycopg.Connection:
    """Open a PostgreSQL connection from a registered service endpoint.

    Args:
        service_connection: Resolved endpoint with its injected credential.
        database_name: Database selected for this connection.
        username: Optional role override for password verification.
        password: Optional password override for password verification.
        autocommit: Whether each statement commits immediately.

    Returns:
        An open PostgreSQL connection.

    Raises:
        ValueError: The registered endpoint is not PostgreSQL.
        psycopg.Error: The server rejects the connection.
    """
    if service_connection.engine != "postgresql":
        raise ValueError("A PostgreSQL service connection is required.")
    return psycopg.connect(
        host=service_connection.host,
        port=service_connection.port,
        user=username or service_connection.username,
        password=password or service_connection.password,
        dbname=database_name,
        autocommit=autocommit,
        connect_timeout=10,
    )


def resolve_database_service(
    *,
    private_dir: Path,
    service_name: str,
    expected_engine: DatabaseEngine | None = None,
) -> DatabaseServiceConnection:
    """Resolve service metadata and a password injected in the process env.

    Args:
        private_dir: Private configuration directory.
        service_name: Exact registered database service name.
        expected_engine: Optional required database engine.

    Returns:
        Connection settings including the injected password.

    Raises:
        ValueError: If the service is missing, invalid, mismatched or its secret
            was not injected with ``private_secrets.py run --env``.
        OSError: If the private service file cannot be read.
    """
    service_record = _find_database_service_record(
        private_dir=private_dir,
        service_name=service_name,
        expected_engine=expected_engine,
    )
    injected_password = os.environ.get(service_record.password_env)
    if not injected_password:
        raise ValueError(
            f"Credential {service_record.password_env} is not injected. Run this "
            "command through private_secrets.py with that --env name."
        )
    return DatabaseServiceConnection(
        service_name=service_record.name,
        engine=service_record.type,
        host=service_record.host,
        port=int(service_record.port),
        database=service_record.database,
        username=service_record.username,
        password=injected_password,
        password_env=service_record.password_env,
    )


def resolve_service_record(
    *, private_dir: Path, service_name: str, expected_engine: DatabaseEngine
) -> ServiceConfig:
    """Resolve a database service record without reading its password.

    Args:
        private_dir: Private configuration directory.
        service_name: Exact registered service name.
        expected_engine: Required database engine.

    Returns:
        Validated service metadata.

    Raises:
        ValueError: If the service is missing or uses another service type.
        OSError: If the private service file cannot be read.
    """
    return _find_database_service_record(
        private_dir=private_dir,
        service_name=service_name,
        expected_engine=expected_engine,
    )


def _find_database_service_record(
    *,
    private_dir: Path,
    service_name: str,
    expected_engine: DatabaseEngine | None,
) -> ServiceConfig:
    """Find and validate the public metadata shared by database commands."""
    services_config = load_services(private_dir / "services.toml")
    matching_services = [
        service_record
        for service_record in services_config.services
        if service_record.name == service_name
    ]
    if not matching_services:
        raise ValueError(f"Database service {service_name!r} is not registered.")
    service_record = matching_services[0]
    if service_record.type not in {"postgresql", "mysql"}:
        raise ValueError(
            f"Service {service_name!r} is {service_record.type!r}, not a database."
        )
    if expected_engine is not None and service_record.type != expected_engine:
        raise ValueError(
            f"Service {service_name!r} uses {service_record.type}; "
            f"expected {expected_engine}."
        )
    if not all(
        (
            service_record.host,
            service_record.port,
            service_record.database,
            service_record.username,
        )
    ):
        raise ValueError(
            f"Service {service_name!r} needs host, port, database and username."
        )
    return service_record
