"""Shared private inventory contract and application target resolution."""

from __future__ import annotations

import ipaddress
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonemptyText = Annotated[str, Field(min_length=1)]
PortNumber = Annotated[int, Field(ge=1, le=65535)]


class InventoryRecord(BaseModel):
    """Reject misspelled fields and implicit type conversions."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SshConfig(InventoryRecord):
    """SSH connection metadata; blank values mean unknown."""

    user: str
    port: PortNumber | Literal[""]
    identity_file: str
    verification_status: Literal["pending", "verified"]

    @model_validator(mode="after")
    def validate_verified(self) -> SshConfig:
        """Require connection details when marked verified.

        Returns:
            Validated SSH configuration.

        Raises:
            ValueError: Verified SSH metadata is incomplete.
        """
        if self.verification_status == "verified" and not all(
            (self.user.strip(), self.port, self.identity_file.strip())
        ):
            raise ValueError("verified SSH requires user, port and identity_file")
        return self


class ApplicationConfig(InventoryRecord):
    """One application deployed on a server."""

    name: NonemptyText
    purpose: str
    url: str
    ports: list[PortNumber]
    deployment_type: str
    deployment_id: str
    deployment_dir: str
    notes: str
    verified_at: str


class ServerConfig(InventoryRecord):
    """Server identity, capacity, connection and application inventory."""

    name: NonemptyText
    public_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | Literal[""]
    private_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | Literal[""]
    cpu_cores: Annotated[int, Field(gt=0)]
    memory_gb: Annotated[int, Field(gt=0)]
    region: NonemptyText
    notes: str
    applications_inventory_status: Literal["pending", "complete"]
    applications: list[ApplicationConfig]
    ssh: SshConfig


class InventoryConfig(InventoryRecord):
    """Versioned private inventory contract."""

    schema_version: Literal[1]
    updated_at: str
    servers: Annotated[list[ServerConfig], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_unique_names(self) -> InventoryConfig:
        """Reject duplicate server names and per-server application names.

        Returns:
            Validated inventory.

        Raises:
            ValueError: Names are duplicated.
        """
        name_groups = [[server.name for server in self.servers]] + [
            [application.name for application in server.applications]
            for server in self.servers
        ]
        if any(len(names) != len(set(names)) for names in name_groups):
            raise ValueError("duplicate server or application names")
        return self


def load_inventory(inventory_path: Path) -> InventoryConfig:
    """Load and validate the same contract used by the inventory checker.

    Args:
        inventory_path: Explicit local TOML path.

    Returns:
        Validated inventory; no network access is performed.

    Raises:
        OSError: File cannot be read.
        ValueError: TOML or inventory fields are invalid.
    """
    inventory_text = inventory_path.read_text(encoding="utf-8")
    try:
        inventory_json = json.dumps(tomllib.loads(inventory_text))
    except TypeError:
        # TOML date/time literals are not valid string fields in this contract.
        raise ValueError("unsupported TOML value type") from None
    return InventoryConfig.model_validate_json(inventory_json)


@dataclass(frozen=True)
class ApplicationTarget:
    """Resolved deployment instance and its containing server."""

    server: ServerConfig
    application: ApplicationConfig


def resolve_application(
    inventory: InventoryConfig, application_name: str, server_name: str | None
) -> ApplicationTarget:
    """Resolve exactly one application instance without guessing a server.

    Args:
        inventory: Validated local inventory.
        application_name: Exact application name.
        server_name: Optional exact server name to disambiguate deployments.

    Returns:
        Unique matching deployment.

    Raises:
        ValueError: Target is missing or ambiguous.
    """
    matching_targets = [
        ApplicationTarget(server=server, application=application)
        for server in inventory.servers
        if server_name is None or server.name == server_name
        for application in server.applications
        if application.name == application_name
    ]
    if len(matching_targets) != 1:
        raise ValueError(
            "target_not_found" if not matching_targets else "target_ambiguous"
        )
    return matching_targets[0]
