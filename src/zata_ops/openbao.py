"""Narrow HTTPS client for the OpenBao KV v2 operations used by zata-ops."""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_RESPONSE = 8 * 1024 * 1024
_TIMEOUT = 10


class OpenBaoError(ValueError):
    """A sanitized failure reason safe for command output."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KVRecord:
    """A KV v2 value and its service-assigned version."""

    values: dict[str, object]
    version: int


def validate_kv_path(path: str) -> str:
    """Accept only explicit, unescaped KV path segments."""
    if not all(_SEGMENT.fullmatch(segment) for segment in path.split("/")):
        raise OpenBaoError("invalid_kv_path")
    return path


class OpenBaoClient:
    """Read and conditionally write KV entries in one externally chosen profile."""

    def __init__(self, profile_path: Path) -> None:
        """Load the local trust settings and require a process-scoped session."""
        try:
            profile_path = profile_path.expanduser()
            if profile_path.stat().st_size > 16384:
                raise ValueError
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            if set(profile) != {
                "address",
                "ca_file",
                "environment",
                "inventory_mount",
                "credentials_mount",
            }:
                raise ValueError
            if not all(isinstance(entry, str) and entry for entry in profile.values()):
                raise ValueError
            endpoint = urlsplit(profile["address"])
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
                or any(
                    character.isspace() or ord(character) < 32
                    for character in profile["address"]
                )
            ):
                raise ValueError
            self.host = endpoint.hostname
            self.port = endpoint.port
            self.environment = profile["environment"]
            if not _SEGMENT.fullmatch(self.environment):
                raise ValueError
            self.inventory_mount = validate_kv_path(profile["inventory_mount"])
            self.credentials_mount = validate_kv_path(profile["credentials_mount"])
            if self.inventory_mount == self.credentials_mount:
                raise ValueError
            ca_file = profile_path.parent / Path(profile["ca_file"]).expanduser()
            self.tls = ssl.create_default_context(cafile=str(ca_file))
        except (OSError, ValueError, TypeError) as error:
            raise OpenBaoError("invalid_openbao_profile") from error
        self._token = os.environ.get("BAO_TOKEN", "")
        if not self._token:
            raise OpenBaoError("openbao_session_required")
        if not self._token.isascii() or any(
            not 33 <= ord(character) <= 126 for character in self._token
        ):
            raise OpenBaoError("invalid_openbao_session")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        connection = http.client.HTTPSConnection(
            self.host, self.port, context=self.tls, timeout=_TIMEOUT
        )
        headers = {"X-Vault-Token": self._token, "Content-Type": "application/json"}
        try:
            connection.request(
                method,
                path,
                body=json.dumps(payload) if payload is not None else None,
                headers=headers,
            )
            response = connection.getresponse()
            if response.status not in {200, 201}:
                reasons = {
                    400: "openbao_request_rejected",
                    401: "openbao_access_denied",
                    403: "openbao_access_denied",
                    404: "openbao_not_found",
                }
                raise OpenBaoError(reasons.get(response.status, "openbao_unavailable"))
            response_bytes = response.read(_MAX_RESPONSE + 1)
            if len(response_bytes) > _MAX_RESPONSE:
                raise OpenBaoError("openbao_response_too_large")
            envelope = json.loads(response_bytes)
            if not isinstance(envelope, dict) or not isinstance(
                envelope.get("data"), dict
            ):
                raise OpenBaoError("invalid_openbao_response")
            return envelope["data"]
        except ssl.SSLError as error:
            raise OpenBaoError("openbao_tls_failed") from error
        except (OSError, http.client.HTTPException) as error:
            raise OpenBaoError("openbao_unavailable") from error
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
            raise OpenBaoError("invalid_openbao_response") from error
        finally:
            connection.close()

    def _read(self, mount: str, key: str, version: int | None = None) -> KVRecord:
        route = f"/v1/{mount}/data/{self.environment}/{validate_kv_path(key)}"
        payload = self._request(
            "GET", route if version is None else f"{route}?version={version}"
        )
        values = payload.get("data")
        metadata = payload.get("metadata")
        recorded_version = (
            metadata.get("version") if isinstance(metadata, dict) else None
        )
        if (
            not isinstance(values, dict)
            or type(recorded_version) is not int
            or recorded_version < 1
        ):
            raise OpenBaoError("invalid_openbao_response")
        return KVRecord(values, recorded_version)

    def read_inventory(self, version: int | None = None) -> KVRecord:
        """Read the environment's complete inventory at the requested version."""
        return self._read(self.inventory_mount, "registry", version)

    def publish_inventory(
        self, values: dict[str, object], expected_version: int
    ) -> int:
        """Publish a complete inventory using a server-enforced version condition."""
        path = f"/v1/{self.inventory_mount}/data/{self.environment}/registry"
        payload = self._request(
            "POST", path, {"options": {"cas": expected_version}, "data": values}
        )
        version = payload.get("version")
        if type(version) is not int or version != expected_version + 1:
            raise OpenBaoError("invalid_openbao_response")
        return version

    def read_credential(self, reference: str) -> str:
        """Read the single nonempty string value at an environment-relative path."""
        values = self._read(self.credentials_mount, reference).values
        value = values.get("value")
        if (
            set(values) != {"value"}
            or not isinstance(value, str)
            or not value
            or "\x00" in value
        ):
            raise OpenBaoError("invalid_openbao_credential")
        return value
