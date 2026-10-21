"""Validate local server inventory without connecting to servers or printing secrets."""

from __future__ import annotations

import argparse
import ipaddress
import subprocess
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


def main() -> int:
    """Validate a TOML inventory and optional Git privacy boundaries.

    Returns:
        Zero on success, one on invalid configuration.
    """
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "path", nargs="?", default="config/private/servers.toml"
    )
    argument_parser.add_argument(
        "--example", action="store_true", help="Skip private-path Git checks"
    )
    cli_arguments = argument_parser.parse_args()
    inventory_path = Path(cli_arguments.path)
    try:
        inventory_text = inventory_path.read_text(encoding="utf-8")
        # JSON validation permits textual IP addresses with otherwise strict types.
        import json

        inventory_config = InventoryConfig.model_validate_json(
            json.dumps(tomllib.loads(inventory_text))
        )
        if not cli_arguments.example:
            tracked_check = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", str(inventory_path)],
                capture_output=True,
            )
            ignored_check = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", str(inventory_path)],
                capture_output=True,
            )
            if tracked_check.returncode != 1 or ignored_check.returncode != 0:
                raise ValueError("private inventory must be Git-ignored and untracked")
    except (OSError, ValueError, ValidationError) as validation_error:
        # Do not echo TOML contents or Pydantic input values.
        if isinstance(validation_error, ValidationError):
            for field_error in validation_error.errors(include_input=False):
                print(
                    f"Invalid field: {'.'.join(map(str, field_error['loc']))} ({field_error['type']})"
                )
        else:
            print(
                f"Inventory validation failed ({type(validation_error).__name__}); check file, TOML syntax and Git exclusion"
            )
        return 1
    print(
        f"Inventory valid: {len(inventory_config.servers)} server(s); no SSH connections made"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
