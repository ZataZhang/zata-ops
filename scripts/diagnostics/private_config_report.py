"""Print a consolidated, credential-safe view of active private ops config."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from private_secrets import credential_status
from private_services import parse_services_config, summarize_services
from zata_ops.inventory import load_inventory


SENSITIVE_FIELD_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
)


def sanitize_config_value(config_key: str, config_value: Any) -> Any:
    """Redact sensitive fields and free-form notes from a config value.

    Args:
        config_key: Name of the field containing the value.
        config_value: Value to sanitize recursively.

    Returns:
        A JSON-serializable value with credentials and notes removed.
    """
    normalized_key = config_key.lower().replace("-", "_")
    if normalized_key == "notes":
        return "[omitted]"
    if normalized_key.endswith("_configured"):
        return bool(config_value)
    if any(
        sensitive_part in normalized_key for sensitive_part in SENSITIVE_FIELD_PARTS
    ):
        return "[redacted]" if config_value else ""
    if isinstance(config_value, dict):
        return {
            nested_key: sanitize_config_value(nested_key, nested_value)
            for nested_key, nested_value in config_value.items()
        }
    if isinstance(config_value, list):
        return [
            sanitize_config_value(config_key, nested_value)
            for nested_value in config_value
        ]
    if normalized_key == "url" and isinstance(config_value, str):
        try:
            parsed_url = urlsplit(config_value)
            parsed_port = parsed_url.port
        except ValueError:
            return "[redacted URL]"
        if parsed_url.username is None and parsed_url.password is None:
            return urlunsplit(
                (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "")
            )
        if parsed_url.hostname is None:
            return "[redacted URL]"
        host_display = (
            f"[{parsed_url.hostname}]"
            if ":" in parsed_url.hostname
            else parsed_url.hostname
        )
        if parsed_port:
            host_display = f"{host_display}:{parsed_port}"
        return urlunsplit(
            (parsed_url.scheme, f"[redacted]@{host_display}", parsed_url.path, "", "")
        )
    return config_value


def build_private_config_report(
    private_dir: Path, repository_root: Path
) -> dict[str, Any]:
    """Load active private configuration summaries without exposing values.

    Args:
        private_dir: Directory containing local private operational config.
        repository_root: Checkout root for the OpenSandbox validator command.

    Returns:
        Structured report for servers, services, credentials and OpenSandbox.

    Raises:
        OSError: A readable configuration file cannot be accessed.
        ValueError: A config file is invalid or uses a symbolic link.
    """
    if private_dir.is_symlink():
        raise ValueError("private_directory_is_symlink")

    report_sections: dict[str, Any] = {
        "schema_version": 1,
        "operation": "private_config.show",
        "scope": "active config/private records; backups and snapshot contents omitted",
    }
    secret_path = private_dir / "secrets.env"
    configured_credentials = credential_status(secret_path)
    report_sections["credentials"] = {
        "file_present": secret_path.exists(),
        "values_shown": False,
        "names": [
            {"name": credential_name, "configured": is_configured}
            for credential_name, is_configured in sorted(configured_credentials.items())
        ],
    }

    inventory_path = private_dir / "servers.toml"
    if inventory_path.is_symlink():
        raise ValueError("server_inventory_is_symlink")
    if inventory_path.exists():
        inventory_config = load_inventory(inventory_path)
        server_records = [
            sanitize_config_value("server", server_record.model_dump(mode="json"))
            for server_record in inventory_config.servers
        ]
        report_sections["servers"] = {
            "status": "loaded",
            "updated_at": inventory_config.updated_at,
            "count": len(server_records),
            "records": server_records,
        }
    else:
        report_sections["servers"] = {"status": "not_configured", "records": []}

    services_path = private_dir / "services.toml"
    if services_path.is_symlink():
        raise ValueError("service_inventory_is_symlink")
    if services_path.exists():
        service_config = parse_services_config(
            services_path.read_text(encoding="utf-8")
        )
        service_records = summarize_services(
            service_config.services, configured_credentials
        )
        report_sections["services"] = {
            "status": "loaded",
            "count": len(service_records),
            "records": [
                sanitize_config_value("service", service_record)
                for service_record in service_records
            ],
        }
    else:
        report_sections["services"] = {"status": "not_configured", "records": []}

    report_sections["opensandbox_profiles"] = load_opensandbox_profiles(
        private_dir / "opensandbox", repository_root
    )
    report_sections["notes"] = (
        "Free-form notes are omitted to reduce accidental disclosure."
    )
    return report_sections


def load_opensandbox_profiles(
    profiles_dir: Path, repository_root: Path
) -> list[dict[str, Any]]:
    """Read OpenSandbox TOML profiles and validate paired env files safely.

    Args:
        profiles_dir: Root directory containing host-specific profiles.
        repository_root: Checkout root for the pinned validation script.

    Returns:
        Profile summaries with sanitized TOML and validation status only.
    """
    profile_summaries = []
    validator_path = repository_root / "scripts/opensandbox/prepare.py"
    for profile_path in sorted(profiles_dir.glob("**/config.toml")):
        path_entry = profile_path
        path_has_symlink = False
        while path_entry != profiles_dir.parent:
            if path_entry.is_symlink():
                path_has_symlink = True
                break
            path_entry = path_entry.parent
        if path_has_symlink:
            profile_summaries.append(
                {"path": str(profile_path), "status": "invalid_symlink"}
            )
            continue
        try:
            profile_config = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            profile_summaries.append(
                {"path": str(profile_path), "status": "invalid_config"}
            )
            continue

        environment_path = profile_path.parent / "server.env"
        validation_status = "missing_server_env"
        if environment_path.exists() and not environment_path.is_symlink():
            try:
                validation_process = subprocess.run(
                    [
                        sys.executable,
                        str(validator_path),
                        "validate",
                        "--config",
                        str(profile_path),
                        "--env-file",
                        str(environment_path),
                    ],
                    cwd=repository_root,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=15,
                    check=False,
                )
                validation_payload = json.loads(validation_process.stdout)
                validation_status = validation_payload.get("status", "invalid_config")
            except (json.JSONDecodeError, subprocess.SubprocessError, OSError):
                validation_status = "invalid_config"
        elif environment_path.is_symlink():
            validation_status = "invalid_env_symlink"

        try:
            display_path = str(profile_path.relative_to(repository_root))
        except ValueError:
            display_path = str(profile_path)
        profile_summaries.append(
            {
                "path": display_path,
                "status": validation_status,
                "config": sanitize_config_value("profile", profile_config),
                "api_key_value_shown": False,
            }
        )
    return profile_summaries


def main() -> int:
    """Print a single JSON report for current private configuration.

    Returns:
        Zero for a complete report or one when a configuration is unreadable.
    """
    repository_root = Path(__file__).resolve().parents[2]
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--private-dir",
        type=Path,
        default=repository_root / "config/private",
        help="Private operational config directory.",
    )
    arguments = argument_parser.parse_args()
    try:
        report = build_private_config_report(arguments.private_dir, repository_root)
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "private_config.show",
                    "status": "error",
                    "reason": "invalid_or_unreadable_private_config",
                    "credential_values_shown": False,
                }
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
