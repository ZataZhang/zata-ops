"""Private service metadata contracts shared by CLI and diagnostics."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from zata_ops.inventory import InventoryRecord

SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
EXPIRY_WARNING_DAYS = 30


class ServiceConfig(InventoryRecord):
    """Public connection metadata and its private credential reference."""

    name: Annotated[str, Field(min_length=1)]
    server: str
    type: Literal["postgresql", "mysql", "redis", "http", "other"]
    host: str
    port: Annotated[int, Field(ge=1, le=65535)] | Literal[""]
    database: str
    username: str
    url: str = ""
    password_env: str
    notes: str
    # 以下为可选的凭据生命周期元数据，主要服务 API Token；数据库密码通常留空。
    scope: str = ""
    created_at: str = ""
    expires_at: str = ""

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_iso_date(cls, value: str) -> str:
        """Require lifecycle dates to be empty or ``YYYY-MM-DD``.

        Args:
            value: Date text stored in the service record.

        Returns:
            The validated date text.

        Raises:
            ValueError: If the text is not an ISO calendar date.
        """
        if value:
            if not ISO_DATE.fullmatch(value):
                raise ValueError("date must be YYYY-MM-DD")
            date.fromisoformat(value)
        return value

    @field_validator("password_env")
    @classmethod
    def validate_password_reference(cls, value: str) -> str:
        """Require references to match private credential environment names.

        Args:
            value: Credential name stored in the service record.

        Returns:
            The validated credential name.

        Raises:
            ValueError: If the reference is not a valid environment name.
        """
        if not SECRET_NAME.fullmatch(value):
            raise ValueError("invalid credential reference")
        return value

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        """Reject URL fields that could contain credentials or query secrets.

        Args:
            value: Public service URL, or an empty string.

        Returns:
            The validated URL.

        Raises:
            ValueError: If the URL contains credentials, a query, or a fragment.
        """
        if value:
            parsed_url = urlsplit(value)
            if (
                not parsed_url.scheme
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError("URL must have no credentials, query or fragment")
            parsed_url.port
        return value


class ServicesConfig(InventoryRecord):
    """Versioned private service registry with unique records and secrets."""

    schema_version: Literal[1] = 1
    services: list[ServiceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_services(self) -> ServicesConfig:
        """Require unique service names and credential references.

        Returns:
            The validated registry.

        Raises:
            ValueError: If names or credential references are duplicated.
        """
        for service_names in (
            [service.name for service in self.services],
            [service.password_env for service in self.services],
        ):
            if len(service_names) != len(set(service_names)):
                raise ValueError("duplicate services or credential references")
        return self


def parse_services_config(service_text: str) -> ServicesConfig:
    """Parse and validate a TOML service registry.

    Args:
        service_text: TOML text from the private service file.

    Returns:
        Validated service registry.

    Raises:
        ValueError: If TOML or service fields are invalid.
    """
    return ServicesConfig.model_validate_json(json.dumps(tomllib.loads(service_text)))


def load_services(service_path: Path) -> ServicesConfig:
    """Load the private service registry without exposing credentials.

    Args:
        service_path: Private TOML service registry.

    Returns:
        Validated registry; an empty registry if the file does not exist.

    Raises:
        ValueError: If the path is a symlink or the file is invalid.
        OSError: If the file cannot be read.
    """
    if service_path.is_symlink():
        raise ValueError("symlink service file")
    if not service_path.exists():
        return ServicesConfig()
    service_path.chmod(0o600)
    return parse_services_config(service_path.read_text(encoding="utf-8"))


def serialize_services_config(services_config: ServicesConfig) -> str:
    """Serialize service records into the established human-readable TOML form.

    Args:
        services_config: Validated service registry.

    Returns:
        UTF-8 text containing metadata only.
    """
    metadata_lines = ["schema_version = 1", ""]
    for service_record in services_config.services:
        metadata_lines.append("[[services]]")
        metadata_lines.extend(
            f"{field_name} = {json.dumps(field_value, ensure_ascii=False)}"
            for field_name, field_value in service_record.model_dump().items()
        )
        metadata_lines.append("")
    return "\n".join(metadata_lines)


def write_services_config(service_path: Path, services_config: ServicesConfig) -> None:
    """Atomically write validated service metadata with private permissions.

    Args:
        service_path: Private TOML service registry.
        services_config: Validated service registry to write.

    Raises:
        OSError: If the temporary or final file cannot be written.
        ValidationError: If the supplied registry is invalid.
    """
    validated_services_config = ServicesConfig.model_validate(services_config)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".services-", dir=service_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(
            temporary_descriptor, "w", encoding="utf-8", closefd=True
        ) as metadata_file:
            metadata_file.write(serialize_services_config(validated_services_config))
        temporary_path.chmod(0o600)
        temporary_path.replace(service_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def credential_expiry(
    service_entry: ServiceConfig, today: date
) -> tuple[str, int | None]:
    """Classify a credential's expiry relative to a given day.

    Args:
        service_entry: Validated service record.
        today: Reference day, injected for deterministic tests.

    Returns:
        ``(status, days_left)`` where status is ``none``, ``ok``, ``expiring``
        (within ``EXPIRY_WARNING_DAYS``) or ``expired``; days_left is None
        when no expiry is recorded.
    """
    if not service_entry.expires_at:
        return "none", None
    days_left = (date.fromisoformat(service_entry.expires_at) - today).days
    if days_left < 0:
        return "expired", days_left
    if days_left <= EXPIRY_WARNING_DAYS:
        return "expiring", days_left
    return "ok", days_left


def summarize_services(
    service_entries: list[ServiceConfig],
    credential_status_by_name: dict[str, bool],
    today: date | None = None,
) -> list[dict[str, object]]:
    """Summarize service metadata and credential availability without values.

    Args:
        service_entries: Selected validated service records.
        credential_status_by_name: Credential-name status map without values.
        today: Reference day for expiry classification; defaults to today.

    Returns:
        Service dictionaries with ``password_configured``, ``expiry_status``
        and ``days_until_expiry`` fields.
    """
    reference_day = today or date.today()
    service_summaries: list[dict[str, object]] = []
    for service_entry in service_entries:
        expiry_status, days_until_expiry = credential_expiry(
            service_entry, reference_day
        )
        service_summaries.append(
            {
                **service_entry.model_dump(),
                "password_configured": credential_status_by_name.get(
                    service_entry.password_env, False
                ),
                "expiry_status": expiry_status,
                "days_until_expiry": days_until_expiry,
            }
        )
    return service_summaries
