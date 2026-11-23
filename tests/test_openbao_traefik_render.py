"""Verify the OpenBao Traefik renderer using synthetic instance settings."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = REPOSITORY_ROOT / "deploy/openbao/traefik/render_dynamic.py"


@pytest.fixture
def invoke_renderer(tmp_path):
    config_path = tmp_path / "ingress.env"
    output_path = tmp_path / "openbao-api.yml"

    def invoke(source_cidrs: str, domain: str = "openbao.example.com"):
        config_path.write_text(
            f"OPENBAO_INGRESS_DOMAIN={domain}\n"
            f"OPENBAO_ALLOWED_SOURCE_CIDRS={source_cidrs}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                str(RENDERER_PATH),
                "--config",
                str(config_path),
                "--gateway",
                "172.20.0.1",
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPOSITORY_ROOT,
            env=os.environ.copy(),
            timeout=10,
            check=False,
        )

    return invoke, output_path


def test_renderer_supports_dokploy_and_multiple_team_sources(invoke_renderer):
    invoke, output_path = invoke_renderer
    rendered = invoke("172.20.0.10/32, 192.0.2.45/32, 2001:db8::45/128")

    assert rendered.returncode == 0, rendered.stderr
    assert "3 allowed sources" in rendered.stdout
    route_text = output_path.read_text(encoding="utf-8")
    assert '          - "172.20.0.10/32"' in route_text
    assert '          - "192.0.2.45/32"' in route_text
    assert '          - "2001:db8::45/128"' in route_text
    assert "openbao-source-allowlist" in route_text
    assert "@" not in route_text
    assert output_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "source_cidrs",
    ["", "192.0.2.45/32,not-a-cidr", "192.0.2.45/32,"],
)
def test_renderer_rejects_invalid_source_cidr_without_echoing_values(
    invoke_renderer, source_cidrs
):
    invoke, _ = invoke_renderer
    rendered = invoke(source_cidrs)

    assert rendered.returncode == 2
    assert "invalid_source_cidr_list" in rendered.stderr
    assert "not-a-cidr" not in rendered.stderr


def test_renderer_rejects_host_rule_injection(invoke_renderer):
    invoke, _ = invoke_renderer
    rendered = invoke("192.0.2.45/32", "openbao.example.com`evil")

    assert rendered.returncode == 2
    assert "invalid_ingress_domain" in rendered.stderr
