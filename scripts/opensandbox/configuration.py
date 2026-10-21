"""Shared, offline policy checks for the private OpenSandbox pilot profile."""

from __future__ import annotations

import re
import tomllib
import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_ROOT = Path("/var/lib/opensandbox")
NETWORK_NAME = "zata-ops-sandbox-isolated"
RESTRICTED_DATA_PATHS = (
    Path("/etc"),
    Path("/home"),
    Path("/opt"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sys"),
    Path("/usr"),
    Path("/var/lib/docker"),
)


def normalize_data_root(data_root: Path) -> Path:
    """Validate a dedicated absolute storage directory for this service.

    Args:
        data_root: Requested host directory for state and workspaces.

    Returns:
        Canonical absolute path without unresolved symbolic links.

    Raises:
        ValueError: The path is relative, unsafe, or has existing symlink components.
    """
    if not data_root.is_absolute():
        raise ValueError("data_root_must_be_absolute")
    if data_root.name != "opensandbox" or any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
        for part in data_root.parts[1:]
    ):
        raise ValueError("data_root_must_end_in_opensandbox")
    normalized_path = Path(os.path.normpath(str(data_root)))
    if normalized_path.is_symlink():
        raise ValueError("data_root_symlink_not_allowed")
    resolved_path = normalized_path.resolve(strict=False)
    if normalized_path == Path("/"):
        raise ValueError("data_root_cannot_be_filesystem_root")
    if any(
        candidate_path == restricted_path
        or candidate_path.is_relative_to(restricted_path)
        or restricted_path.is_relative_to(candidate_path)
        for candidate_path in (normalized_path, resolved_path)
        for restricted_path in RESTRICTED_DATA_PATHS
    ):
        raise ValueError("data_root_overlaps_restricted_path")
    return normalized_path


@dataclass(frozen=True)
class DeploymentSettings:
    """Validated deployment inputs; never print the authentication key."""

    config: dict
    api_key: str = field(repr=False)
    config_path: Path = field(repr=False)
    env_path: Path = field(repr=False)
    example: bool = False


def is_pinned_image(image_reference: object) -> bool:
    """Check whether an OCI reference is pinned to a SHA-256 digest.

    Args:
        image_reference: Untrusted configuration or CLI value.

    Returns:
        Whether the reference carries an immutable digest.
    """
    return isinstance(image_reference, str) and bool(
        re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image_reference)
    )


def validate_configuration(config: dict, data_root: Path = DATA_ROOT) -> list[str]:
    """Check invariants of the owned pilot profile, without Docker access.

    Args:
        config: Parsed upstream-compatible configuration dictionary.
        data_root: Dedicated persistent directory for service state and workspaces.

    Returns:
        Stable violation codes; contains no configuration values or secrets.
    """
    try:
        normalized_data_root = normalize_data_root(data_root)
    except ValueError:
        return ["invalid_data_root"]
    required_values = {
        ("server", "host"): "127.0.0.1",
        ("server", "port"): 18080,
        ("runtime", "type"): "docker",
        ("secure_runtime", "type"): "gvisor",
        ("secure_runtime", "docker_runtime"): "runsc",
        ("proxy", "resolve_internal"): True,
        ("docker", "publish_host"): "127.0.0.1",
        ("docker", "network_mode"): NETWORK_NAME,
        ("docker", "no_new_privileges"): True,
        ("docker", "drop_capabilities"): ["ALL"],
        ("store", "type"): "sqlite",
        ("store", "path"): str(normalized_data_root / "control-plane/opensandbox.db"),
        ("storage", "allowed_host_paths"): [str(normalized_data_root / "workspaces")],
        ("ingress", "mode"): "direct",
        ("renew_intent", "enabled"): False,
    }
    violations = []
    for (section_name, field_name), expected in required_values.items():
        section = config.get(section_name, {})
        if not isinstance(section, dict) or section.get(field_name) != expected:
            violations.append(f"invalid_{section_name}_{field_name}")
    # Bounds are fixed for this profile; changes require reviewing port/firewall policy.
    bounded_values = {
        ("server", "max_sandbox_timeout_seconds"): 900,
        ("docker", "pids_limit"): 256,
        ("docker", "port_range_min"): 41000,
        ("docker", "port_range_max"): 41100,
    }
    allowed_fields: dict[str, set[str]] = {}
    for section_name, field_name in (*required_values, *bounded_values):
        allowed_fields.setdefault(section_name, set()).add(field_name)
    allowed_fields["runtime"].add("execd_image")
    allowed_fields["log"] = {"level", "file_enabled"}
    for section_name, section in config.items():
        if (
            section_name not in allowed_fields
            or not isinstance(section, dict)
            or set(section) - allowed_fields[section_name]
        ):
            violations.append("unreviewed_configuration_fields")
    for (section_name, field_name), expected in bounded_values.items():
        section = config.get(section_name, {})
        actual = section.get(field_name) if isinstance(section, dict) else None
        if type(actual) is not int or actual != expected:
            violations.append(f"invalid_{section_name}_{field_name}")
    runtime = config.get("runtime", {})
    image_reference = (
        runtime.get("execd_image", "") if isinstance(runtime, dict) else ""
    )
    if not is_pinned_image(image_reference):
        violations.append("execd_digest_required")
    server = config.get("server", {})
    if isinstance(server, dict) and "api_key" in server:
        violations.append("api_key_must_use_environment_file")
    return violations


def load_settings(
    config_path: Path,
    env_path: Path,
    *,
    example: bool = False,
    data_root: Path = DATA_ROOT,
) -> DeploymentSettings:
    """Load exact deployment policy and a restricted systemd environment file.

    Args:
        config_path: Local TOML file.
        env_path: Local file containing only OPENSANDBOX_SERVER_API_KEY.
        example: Allow the shipped dummy key for offline example validation only.
        data_root: Expected host path for SQLite state and sandbox workspaces.

    Returns:
        Validated deployment inputs.

    Raises:
        OSError: Files cannot be read.
        ValueError: Configuration violates policy or the key is invalid.
    """
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    violations = validate_configuration(config, data_root=data_root)
    environment = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        key, separator, value = stripped_line.partition("=")
        if (
            separator != "="
            or key != "OPENSANDBOX_SERVER_API_KEY"
            or key in environment
        ):
            raise ValueError("invalid_environment_file")
        environment[key] = value
    api_key = environment.get("OPENSANDBOX_SERVER_API_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{64}", api_key):
        if not example or api_key != "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS":
            violations.append("invalid_api_key")
    if not example:
        for private_path in (config_path, env_path):
            if private_path.stat().st_mode & 0o077:
                violations.append("private_file_permissions_required")
    if violations:
        raise ValueError(",".join(violations))
    return DeploymentSettings(
        config=config,
        api_key=api_key,
        config_path=config_path,
        env_path=env_path,
        example=example,
    )
