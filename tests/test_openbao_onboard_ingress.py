"""Cover source-list drift and interrupted OpenBao onboarding."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "deploy/openbao/traefik"
EXISTING = "192.0.2.10/32"
NEW = "192.0.2.11/32"


@pytest.fixture
def ingress_module(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT_DIR))
    import onboard_ingress

    return onboard_ingress


@pytest.fixture
def private_ingress(tmp_path):
    ingress_path = tmp_path / "ingress.env"
    ingress_path.write_text(
        "OPENBAO_INGRESS_DOMAIN=openbao.example.com\n"
        f"OPENBAO_ALLOWED_SOURCE_CIDRS={EXISTING}\n",
        encoding="utf-8",
    )
    return ingress_path


def _route(*networks):
    return yaml.safe_dump(
        {
            "http": {
                "routers": {
                    "openbao-api": {
                        "rule": "Host(`openbao.example.com`) && PathPrefix(`/v1/`)",
                        "middlewares": ["legacy-openbao-allowlist"],
                    }
                },
                "middlewares": {
                    "legacy-openbao-allowlist": {
                        "ipAllowList": {"sourceRange": list(networks)}
                    }
                },
                "services": {"openbao-api": {"loadBalancer": {"servers": []}}},
            }
        }
    ).encode()


def test_onboarding_can_resume_after_live_update(ingress_module, private_ingress):
    live_route = _route(EXISTING)
    change = ingress_module.IngressChange.plan(private_ingress, live_route, NEW)
    assert yaml.safe_load(change.route_update) == yaml.safe_load(_route(EXISTING, NEW))
    resumed = ingress_module.IngressChange.plan(
        private_ingress, change.route_update, NEW
    )
    assert resumed.route_update is None
    resumed.sync_inventory(private_ingress)
    repeated = ingress_module.IngressChange.plan(
        private_ingress, change.route_update, NEW
    )
    assert repeated.route_update is None
    assert repeated.inventory_update is None


def test_onboarding_rejects_unrelated_live_drift(ingress_module, private_ingress):
    with pytest.raises(ValueError, match="differ beyond the requested IP"):
        ingress_module.IngressChange.plan(
            private_ingress, _route(EXISTING, "192.0.2.12/32"), NEW
        )


def test_route_installer_checks_current_revision(ingress_module, tmp_path):
    live_path = tmp_path / "openbao-api.yml"
    original, updated = _route(EXISTING), _route(EXISTING, NEW)
    live_path.write_bytes(original)
    command = [
        sys.executable,
        "-c",
        ingress_module.INSTALL_ROUTE,
        str(live_path),
        hashlib.sha256(original).hexdigest(),
    ]
    subprocess.run(command, input=updated, check=True, timeout=10)
    assert live_path.read_bytes() == updated
    rejected = subprocess.run(command, input=original, check=False, timeout=10)
    assert rejected.returncode == 3
    assert live_path.read_bytes() == updated
