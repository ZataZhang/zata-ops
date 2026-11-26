"""Validate and resolve one environment snapshot from OpenBao."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from zata_ops.deploy.targets import DeployTarget, parse_deploy_targets
from zata_ops.inventory import InventoryConfig, parse_inventory
from zata_ops.openbao import KVRecord, OpenBaoClient, OpenBaoError, validate_kv_path
from zata_ops.services import ServicesConfig, parse_services_config

ACTIVE_PROFILE: ContextVar[Path | None] = ContextVar("zata_ops_profile", default=None)
DOCUMENTS = ("servers.toml", "services.toml", "deploy_targets.toml")


def _validate_endpoint(endpoint: str) -> None:
    if not endpoint:
        return
    address = urlsplit(endpoint)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username is not None
        or address.password is not None
        or address.query
        or address.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError
    _ = address.port


@dataclass(frozen=True)
class RemoteInventory:
    """Validated records that share one OpenBao KV version."""

    record: KVRecord
    inventory: InventoryConfig
    services: ServicesConfig
    targets: dict[str, DeployTarget]

    @classmethod
    def parse(cls, record: KVRecord) -> RemoteInventory:
        """Validate all documents and their references as one snapshot."""
        if set(record.values) != set(DOCUMENTS) or not all(
            isinstance(document, str) for document in record.values.values()
        ):
            raise OpenBaoError("invalid_remote_inventory")
        documents = cast(dict[str, str], record.values)
        try:
            inventory = parse_inventory(documents["servers.toml"])
            services = parse_services_config(documents["services.toml"])
            targets = parse_deploy_targets(
                documents["deploy_targets.toml"], allow_empty=True
            )
            server_names = {server.name for server in inventory.servers}
            for server in inventory.servers:
                if server.ssh.identity_file:
                    validate_kv_path(server.ssh.identity_file)
                    if "/" in server.ssh.identity_file:
                        raise ValueError
                for application in server.applications:
                    _validate_endpoint(application.url)
            for service in services.services:
                validate_kv_path(service.credential_ref)
                if service.server and service.server not in server_names:
                    raise ValueError
            for target in targets.values():
                _validate_endpoint(target.health_url)
        except (ValueError, TypeError, RecursionError) as error:
            raise OpenBaoError("invalid_remote_inventory") from error
        return cls(record, inventory, services, targets)

    @classmethod
    def from_directory(cls, directory: Path) -> RemoteInventory:
        """Read the three required input documents and validate their snapshot."""
        values: dict[str, object] = {}
        try:
            for name in DOCUMENTS:
                document_path = directory / name
                if (
                    document_path.is_symlink()
                    or document_path.stat().st_size > 1024 * 1024
                ):
                    raise ValueError
                values[name] = document_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise OpenBaoError("invalid_publish_directory") from error
        return cls.parse(KVRecord(values, 0))


def remote_client(*, local_override: bool = False) -> OpenBaoClient | None:
    """Resolve the explicitly selected remote profile without fallback."""
    profile_path = ACTIVE_PROFILE.get()
    if profile_path is None:
        return None
    if local_override:
        raise OpenBaoError("remote_profile_conflicts_with_local_override")
    return OpenBaoClient(profile_path)


def require_remote_client() -> OpenBaoClient:
    """Require an explicit remote profile for remote-only commands."""
    client = remote_client()
    if client is None:
        raise OpenBaoError("remote_profile_required")
    return client
