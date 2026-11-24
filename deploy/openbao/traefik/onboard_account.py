"""Create and verify a FreshAI development Userpass account over a trusted tunnel."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

READ_POLICY = "app-freshai-dev-read"
DEVELOPMENT_PATH = "kv/data/dev/freshai/runtime"
TOKEN_TTL = 24 * 60 * 60
MAX_TOKEN_TTL = 7 * TOKEN_TTL


class _BaoError(RuntimeError):
    def __init__(self, path: str, stderr: bytes, stdout: bytes) -> None:
        http_status = re.search(rb"Code: (\d+)", stderr)
        self.status = int(http_status[1]) if http_status else None
        self.missing = self.status == 404 or any(
            stream.startswith(b"No value found") for stream in (stdout, stderr)
        )
        detail = f"HTTP {self.status}" if self.status else "command failed"
        super().__init__(f"OpenBao {path}: {detail}")


@dataclass(frozen=True)
class _BaoSession:
    environment: dict[str, str]
    token: str = ""

    def call(self, arguments: list[str], *, fields: dict | None = None) -> bytes:
        completed = subprocess.run(
            ["bao", *arguments, *(["-"] if fields is not None else [])],
            env=self.environment | {"BAO_TOKEN": self.token},
            input=json.dumps(fields).encode() if fields is not None else b"",
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode:
            raise _BaoError(arguments[-1], completed.stderr, completed.stdout)
        return completed.stdout

    def read(self, path: str) -> dict:
        return json.loads(self.call(["read", "-format=json", path]))["data"]

    def login(self, stack: ExitStack, path: str, fields: dict) -> _BaoSession:
        token_bytes = self.call(["write", "-field=token", path], fields=fields)
        session = _BaoSession(self.environment, token_bytes.decode().strip())
        stack.callback(session.call, ["token", "revoke", "-self"])
        return session


@dataclass(frozen=True)
class UserpassAccount:
    """Private inputs for a single FreshAI development account."""

    instance_dir: Path
    username: str
    identity: Path

    @property
    def archive(self) -> Path:
        """Return the local encrypted initial password file."""
        return self.instance_dir / "users" / f"{self.username}.json.age"

    def _archive_password(self, path: Path) -> str:
        decrypted_json = subprocess.check_output(
            ["age", "-d", "-i", str(self.identity), str(path)], timeout=30
        )
        password = json.loads(decrypted_json)["password"]
        if not isinstance(password, str) or not password:
            raise ValueError("Invalid private password archive.")
        return password

    def _prepare_password(self) -> str:
        if self.archive.exists():
            return self._archive_password(self.archive)
        self.archive.parent.mkdir(mode=0o700, exist_ok=True)
        password = secrets.token_urlsafe(32)
        encrypted_input = json.dumps({"username": self.username, "password": password})
        public_key = (
            subprocess.check_output(
                ["ssh-keygen", "-y", "-f", str(self.identity)],
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            .decode()
            .strip()
        )
        with tempfile.TemporaryDirectory(dir=self.archive.parent) as staging_dir:
            staged_archive = Path(staging_dir) / self.archive.name
            subprocess.run(
                ["age", "-r", public_key, "-o", str(staged_archive)],
                input=encrypted_input.encode(),
                capture_output=True,
                check=True,
                timeout=30,
            )
            staged_archive.chmod(0o600)
            if self._archive_password(staged_archive) != password:
                raise RuntimeError("Encrypted password verification failed.")
            os.link(staged_archive, self.archive)
        return password

    def provision(self, local_port: int, scratch_dir: Path) -> None:
        """Ensure the account exists and validate access using its archived password.

        Args:
            local_port: Loopback port forwarded to OpenBao over SSH.
            scratch_dir: Private directory isolating the CLI token helper path.

        Raises:
            RuntimeError: OpenBao refused the operation or account validation failed.
            ValueError: The live policy or private password archive is invalid.
        """
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BAO_", "VAULT_"))
        }
        unauthenticated = _BaoSession(
            environment
            | {
                "BAO_ADDR": f"https://127.0.0.1:{local_port}",
                "BAO_CACERT": str(self.instance_dir / "server.crt"),
                "BAO_TOKEN_PATH": str(scratch_dir / "unused-token"),
            }
        )
        user_path = f"auth/userpass/users/{self.username}"
        temporary_name = f"app-{self.username}-provision-{secrets.token_hex(8)}"
        role_path = f"auth/approle/role/{temporary_name}"
        policy_path = f"sys/policies/acl/{temporary_name}"
        with ExitStack() as cleanup:
            operator = unauthenticated.login(
                cleanup,
                "auth/userpass/login/operator",
                {
                    "password": self._archive_password(
                        self.instance_dir / "operator.json.age"
                    )
                },
            )
            policy = operator.read(f"sys/policies/acl/{READ_POLICY}")["policy"]
            pattern = rf'path\s+"{DEVELOPMENT_PATH}"\s*\{{\s*capabilities\s*=\s*\[\s*"read"\s*\]\s*\}}'
            if not re.fullmatch(pattern, policy.strip()):
                raise ValueError(
                    "Live FreshAI development policy differs from the expected read-only scope."
                )
            cleanup.callback(operator.call, ["delete", policy_path])
            operator.call(
                ["write", policy_path],
                fields={
                    "policy": f'path "{user_path}" {{ capabilities = ["create", "read"] }}'
                },
            )
            cleanup.callback(operator.call, ["delete", role_path])
            operator.call(
                ["write", role_path],
                fields={
                    "bind_secret_id": True,
                    "secret_id_ttl": "2m",
                    "secret_id_num_uses": 1,
                    "token_policies": [temporary_name],
                    "token_ttl": "5m",
                    "token_max_ttl": "5m",
                    "token_explicit_max_ttl": "5m",
                    "token_type": "service",
                },
            )
            role_id = operator.read(f"{role_path}/role-id")["role_id"]
            secret_id = operator.call(
                ["write", "-f", "-field=secret_id", f"{role_path}/secret-id"]
            )
            provisioner = unauthenticated.login(
                cleanup,
                "auth/approle/login",
                {
                    "role_id": role_id,
                    "secret_id": secret_id.decode().strip(),
                },
            )
            try:
                account_settings = provisioner.read(user_path)
            except _BaoError as error:
                if not error.missing:
                    raise
                account_settings = None
            if account_settings is None:
                password = self._prepare_password()
                provisioner.call(
                    ["write", user_path],
                    fields={
                        "password": password,
                        "token_policies": [READ_POLICY],
                        "token_ttl": TOKEN_TTL,
                        "token_max_ttl": MAX_TOKEN_TTL,
                        "token_explicit_max_ttl": MAX_TOKEN_TTL,
                    },
                )
                account_settings = provisioner.read(user_path)
            elif self.archive.is_file():
                password = self._archive_password(self.archive)
            else:
                raise RuntimeError(
                    "Username already exists without a local password archive."
                )
            if (
                account_settings["token_policies"] != [READ_POLICY]
                or account_settings["token_ttl"] != TOKEN_TTL
                or account_settings["token_explicit_max_ttl"] != MAX_TOKEN_TTL
            ):
                raise RuntimeError(
                    "Account policy or token lifetime differs from the expected settings."
                )
            personal = unauthenticated.login(
                cleanup,
                f"auth/userpass/login/{self.username}",
                {
                    "password": password,
                },
            )
            token_details = json.loads(
                personal.call(["token", "lookup", "-format=json"])
            )["data"]
            if set(token_details["policies"]) != {
                "default",
                READ_POLICY,
            } or token_details.get("identity_policies"):
                raise RuntimeError("The account has unexpected effective policies.")
            personal.call(["read", "-field=metadata", DEVELOPMENT_PATH])
            for stage in ("test", "prod"):
                try:
                    personal.call(
                        ["read", "-field=metadata", f"kv/data/{stage}/freshai/runtime"]
                    )
                except _BaoError as error:
                    if error.status != 403:
                        raise
                else:
                    raise RuntimeError(f"OpenBao unexpectedly allowed {stage} reads.")
