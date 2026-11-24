"""Plan and apply the OpenBao API router's source allowlist change."""

from __future__ import annotations

import hashlib
import ipaddress
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import set_key

from render_dynamic import load_ingress_settings, parse_allowed_networks

ROUTE_PATH = "/opt/traefik/dynamic/openbao-api.yml"
SOURCE_KEY = "OPENBAO_ALLOWED_SOURCE_CIDRS"
INSTALL_ROUTE = """\
import fcntl
import hashlib
import os
import sys
import tempfile
from pathlib import Path

route_path = Path(sys.argv[1])
with route_path.with_name("." + route_path.name + ".lock").open("a") as route_lock:
    fcntl.flock(route_lock, fcntl.LOCK_EX)
    if hashlib.sha256(route_path.read_bytes()).hexdigest() != sys.argv[2]:
        sys.exit(3)
    with tempfile.NamedTemporaryFile(dir=route_path.parent, prefix=".openbao-", delete=False) as pending:
        pending_path = Path(pending.name)
        try:
            pending.write(sys.stdin.buffer.read())
            pending.flush()
            os.fchmod(pending.fileno(), route_path.stat().st_mode & 0o777)
            os.replace(pending_path, route_path)
        finally:
            pending_path.unlink(missing_ok=True)
"""


def validate_public_ip(value: str) -> str:
    """Convert one public unicast address to a host CIDR.

    Args:
        value: The colleague's public egress IP.

    Returns:
        An IPv4 /32 or IPv6 /128.

    Raises:
        ValueError: The input is not an individual global unicast IP.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(
            "Enter one public IPv4 or IPv6 address without a prefix."
        ) from None
    if (
        not address.is_global
        or address.is_multicast
        or getattr(address, "scope_id", None)
    ):
        raise ValueError("Enter a public unicast IP address.")
    return f"{address}/{address.max_prefixlen}"


@dataclass(frozen=True)
class IngressChange:
    """The verified Traefik and private inventory updates for one IP."""

    live_digest: str
    route_update: bytes | None
    inventory_update: str | None

    @classmethod
    def plan(cls, ingress_path: Path, live_route: bytes, network: str) -> IngressChange:
        """Plan a dedicated middleware edit while preserving other settings.

        Args:
            ingress_path: Private dotenv inventory for the selected instance.
            live_route: Current dynamic YAML read over SSH.
            network: Validated single-host CIDR.

        Returns:
            Pending changes for each side. A missing update is already applied.

        Raises:
            ValueError: The route is unsupported or unrelated sources have drifted.
        """
        domain, inventory_networks = load_ingress_settings(ingress_path)
        try:
            route_document = yaml.safe_load(live_route)
            if set(route_document) != {"http"}:
                raise ValueError
            configuration = route_document["http"]
            routers = configuration["routers"]
            middlewares = configuration["middlewares"]
            router = routers["openbao-api"]
            if router["rule"] != f"Host(`{domain}`) && PathPrefix(`/v1/`)":
                raise ValueError
            (name,) = router["middlewares"]
            allowlist = middlewares[name]["ipAllowList"]
            if any(
                name in other_router.get("middlewares", [])
                for router_name, other_router in routers.items()
                if router_name != "openbao-api"
            ) or any(
                name in middleware.get("chain", {}).get("middlewares", [])
                for middleware in middlewares.values()
            ):
                raise ValueError
            source_ranges = allowlist["sourceRange"]
            if not isinstance(source_ranges, list):
                raise ValueError
            live_networks = parse_allowed_networks(",".join(source_ranges))
        except (yaml.YAMLError, KeyError, TypeError, AttributeError, ValueError):
            raise ValueError(
                "Expected the dedicated OpenBao API router and IP allowlist."
            ) from None
        if set(inventory_networks) - {network} != set(live_networks) - {network}:
            raise ValueError(
                "Live and local allowlists differ beyond the requested IP. Reconcile them first."
            )
        # Keep the live middleware name, TLS configuration and upstream settings.
        route_update = None
        if network not in live_networks:
            allowlist["sourceRange"] = [*source_ranges, network]
            route_update = yaml.safe_dump(route_document, sort_keys=False).encode(
                "utf-8"
            )
        inventory_update = None
        if network not in inventory_networks:
            inventory_update = ",".join([*inventory_networks, network])
        return cls(
            hashlib.sha256(live_route).hexdigest(), route_update, inventory_update
        )

    def sync_inventory(self, ingress_path: Path) -> None:
        """Update the private source list after the live route is confirmed.

        Args:
            ingress_path: The instance's private dotenv file.
        """
        if self.inventory_update:
            with tempfile.TemporaryDirectory(dir=ingress_path.parent) as staging_dir:
                staged_ingress = Path(staging_dir) / ingress_path.name
                staged_ingress.write_text(
                    ingress_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
                set_key(
                    staged_ingress, SOURCE_KEY, self.inventory_update, encoding="utf-8"
                )
                _, staged_networks = load_ingress_settings(staged_ingress)
                if staged_networks != self.inventory_update.split(","):
                    raise ValueError("The staged private allowlist did not verify.")
                staged_ingress.chmod(0o600)
                staged_ingress.replace(ingress_path)
