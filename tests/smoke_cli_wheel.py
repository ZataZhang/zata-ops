"""Exercise the installed wheel using public examples from outside the checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory


def _run_cli(*arguments: str, exit_code: int = 0) -> str:
    cli_path = Path(sys.executable).with_name("zata-ops")
    completed_command = subprocess.run(
        [str(cli_path), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )
    assert completed_command.returncode == exit_code, completed_command
    return completed_command.stdout


def _verify_cli() -> None:
    examples_directory = Path(__file__).resolve().parents[1] / "config"
    package_version = distribution("zata-ops").version
    distribution("pymysql")
    assert _run_cli("--version").strip() == f"zata-ops {package_version}"
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        assert os.environ["GITHUB_REF_NAME"] == f"v{package_version}"
    for template_name in (
        "bootstrap.sh",
        "fix-acme-email.sh",
        "install-docker-traefik.sh",
    ):
        assert files("zata_ops.env.templates").joinpath(template_name).is_file()

    with TemporaryDirectory(dir=Path.cwd()) as directory_name:
        private_directory = Path(directory_name)
        inventory_path = private_directory / "servers.toml"
        services_path = private_directory / "services.toml"
        shutil.copyfile(examples_directory / "servers.example.toml", inventory_path)
        shutil.copyfile(examples_directory / "services.example.toml", services_path)
        services_path.chmod(0o640)
        fake_secret = "wheel-smoke-dummy-secret"
        (private_directory / "secrets.env").write_text(
            f"MAIN_POSTGRES_PASSWORD={fake_secret}\n", encoding="utf-8"
        )

        os.environ["ZATA_OPS_PRIVATE_DIR"] = str(private_directory / "unused")
        doctor_report = json.loads(_run_cli("doctor", "--private-dir", directory_name))
        assert doctor_report["version"] == package_version
        assert doctor_report["private_dir"] == directory_name
        assert doctor_report["status"] == "ok"
        assert (
            json.loads(_run_cli("doctor", exit_code=1))["checks"]["servers.toml"]
            == "missing"
        )
        preview_arguments = ("app", "inspect", "example-app", "--dry-run")
        assert (
            json.loads(
                _run_cli(*preview_arguments, "--inventory", str(inventory_path))
            )["status"]
            == "planned"
        )

        os.environ["ZATA_OPS_PRIVATE_DIR"] = directory_name
        service_list = json.loads(_run_cli("service", "list"))
        assert service_list["schema_version"] == 1
        assert len(service_list["services"]) == 2
        service_json = _run_cli("service", "show", "main-postgres")
        assert fake_secret not in service_json
        assert json.loads(service_json)["services"][0]["password_configured"] is True
        assert services_path.stat().st_mode & 0o777 == 0o640
        assert (
            json.loads(_run_cli("service", "show", "unknown", exit_code=1))["reason"]
            == "service_not_found"
        )
        assert json.loads(_run_cli(*preview_arguments))["status"] == "planned"

        services_path.write_text("schema_version = 2026-01-01\n", encoding="utf-8")
        assert (
            json.loads(_run_cli("doctor", exit_code=1))["checks"]["services.toml"]
            == "invalid"
        )
        assert json.loads(_run_cli("service", "list", exit_code=2))["status"] == "error"


if __name__ == "__main__":
    _verify_cli()
