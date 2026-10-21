"""Offline deployment gates and smoke-probe failure/cleanup behavior."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO_ROOT / "deploy/opensandbox"
SCRIPTS_ROOT = REPO_ROOT / "scripts/opensandbox"


@pytest.fixture
def prep_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS_ROOT))
    return SimpleNamespace(
        config=importlib.import_module("configuration"),
        configure=importlib.import_module("configure"),
        install=importlib.import_module("install"),
        runtime=importlib.import_module("runtime"),
        smoke=importlib.import_module("smoke"),
    )


@pytest.fixture
def private_files(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (PROFILE_ROOT / "config.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env_path = tmp_path / "server.env"
    env_path.write_text(
        "OPENSANDBOX_SERVER_API_KEY=" + "a" * 64 + "\n", encoding="utf-8"
    )
    config_path.chmod(0o600)
    env_path.chmod(0o600)
    return config_path, env_path


def test_real_offline_entrypoints_do_not_invoke_docker(tmp_path, private_files):
    fake_docker = tmp_path / "docker"
    marker_path = tmp_path / "docker-called"
    fake_docker.write_text(
        f'#!/bin/sh\ntouch "{marker_path}"\nexit 99\n', encoding="utf-8"
    )
    fake_docker.chmod(0o700)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    validation = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_ROOT / "prepare.py"),
            "validate",
            "--config",
            str(PROFILE_ROOT / "config.example.toml"),
            "--env-file",
            str(PROFILE_ROOT / "server.env.example"),
            "--example",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=15,
    )
    assert validation.returncode == 0
    assert json.loads(validation.stdout)["deployment_ready"] is False
    preview = subprocess.run(
        [sys.executable, str(SCRIPTS_ROOT / "smoke.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=15,
    )
    assert preview.returncode == 0
    assert json.loads(preview.stdout)["creates_sandbox"] is False
    assert not marker_path.exists()
    install_plan = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_ROOT / "install.py"),
            "--config",
            str(private_files[0]),
            "--env-file",
            str(private_files[1]),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=15,
    )
    assert install_plan.returncode == 0
    assert json.loads(install_plan.stdout)["mutations"] is False
    assert not marker_path.exists()


def test_example_cannot_be_host_readiness_evidence():
    preflight = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_ROOT / "prepare.py"),
            "preflight",
            "--config",
            str(PROFILE_ROOT / "config.example.toml"),
            "--env-file",
            str(PROFILE_ROOT / "server.env.example"),
            "--example",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert preflight.returncode == 2


@pytest.mark.parametrize(
    "replacement",
    [
        ('type = "gvisor"', 'type = ""'),
        ('docker_runtime = "runsc"', 'docker_runtime = "runc"'),
        ('host = "127.0.0.1"', 'host = "0.0.0.0"'),
        ('publish_host = "127.0.0.1"', 'publish_host = "0.0.0.0"'),
        ('network_mode = "zata-ops-sandbox-isolated"', 'network_mode = "host"'),
        ("pids_limit = 256", "pids_limit = 0"),
        (
            'allowed_host_paths = ["/var/lib/opensandbox/workspaces"]',
            "allowed_host_paths = []",
        ),
    ],
)
def test_configuration_rejects_weakened_profile(
    prep_modules, private_files, replacement
):
    config_path, env_path = private_files
    original, weakened = replacement
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(original, weakened),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        prep_modules.config.load_settings(config_path, env_path)


def test_key_and_private_permissions_required(prep_modules, private_files):
    config_path, env_path = private_files
    assert prep_modules.config.load_settings(config_path, env_path).api_key == "a" * 64
    env_path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        prep_modules.config.load_settings(config_path, env_path)
    env_path.chmod(0o600)
    env_path.write_text(
        "OPENSANDBOX_SERVER_API_KEY=REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid_api_key"):
        prep_modules.config.load_settings(config_path, env_path)


def test_invalid_private_values_never_echo(tmp_path):
    config_path = tmp_path / "broken.toml"
    env_path = tmp_path / "secret.env"
    config_path.write_text('not-toml "SECRET_TOKEN"', encoding="utf-8")
    env_path.write_text("OPENSANDBOX_SERVER_API_KEY=SECRET_TOKEN", encoding="utf-8")
    invalid = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_ROOT / "prepare.py"),
            "validate",
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert invalid.returncode == 2
    assert "SECRET_TOKEN" not in invalid.stdout + invalid.stderr


@pytest.mark.parametrize("runtime", ["runsc", "runc", "files_error"])
def test_smoke_proves_runtime_before_io_and_confirms_cleanup(
    prep_modules, monkeypatch, runtime
):
    calls = []
    container_exists = True

    class FakeSandbox:
        id = "fictional-id"

        @classmethod
        async def create(cls, *arguments, **keywords):
            calls.append("create")
            return cls()

        async def kill(self):
            nonlocal container_exists
            calls.append("kill")
            container_exists = False

        async def close(self):
            calls.append("close")

        @property
        def files(self):
            if runtime == "files_error":
                raise RuntimeError("file_probe_failed")
            return self

        async def write_files(self, entries):
            calls.append("write")
            self.saved_text = entries[0]["data"]

        async def read_file(self, path):
            calls.append("read")
            return self.saved_text

        @property
        def commands(self):
            return self

        async def run(self, command, **kwargs):
            calls.append("command")
            return SimpleNamespace(
                exit_code=0,
                error=None,
                logs=SimpleNamespace(stdout=[SimpleNamespace(text=self.saved_text)]),
            )

    monkeypatch.setitem(
        sys.modules, "opensandbox", SimpleNamespace(Sandbox=FakeSandbox)
    )
    monkeypatch.setitem(
        sys.modules,
        "opensandbox.config",
        SimpleNamespace(ConnectionConfig=lambda **kwargs: kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "opensandbox.models",
        SimpleNamespace(WriteEntry=lambda **kwargs: kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "opensandbox.models.execd",
        SimpleNamespace(RunCommandOpts=lambda **kwargs: kwargs),
    )
    monkeypatch.setattr(
        prep_modules.smoke,
        "_owned_containers",
        lambda marker: ["container-id"] if container_exists else [],
    )

    def inspect_container(command, **kwargs):
        assert command[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        container = {
            "HostConfig": {
                "Runtime": "runsc" if runtime == "files_error" else runtime,
                "NetworkMode": "zata-ops-sandbox-isolated",
            },
            "Config": {"Labels": {"opensandbox.io/id": "fictional-id"}},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps([container]), "")

    monkeypatch.setattr(prep_modules.smoke.subprocess, "run", inspect_container)
    report = asyncio.run(
        prep_modules.smoke.run_smoke("a" * 64, "python@sha256:" + "b" * 64)
    )
    assert report["status"] == ("passed" if runtime == "runsc" else "failed")
    assert report["cleanup"] == "confirmed_absent"
    assert calls == (
        ["create", "write", "read", "command", "kill", "close"]
        if runtime == "runsc"
        else ["create", "kill", "close"]
    )
    assert report["checks"]["runtime_runsc"] is (runtime != "runc")


def test_custom_data_root_is_validated_without_changing_other_profile_fields(
    prep_modules,
):
    data_root = Path("/mnt/data/opensandbox")
    profile_config = tomllib.loads(
        (PROFILE_ROOT / "config.example.toml").read_text(encoding="utf-8")
    )
    profile_config["store"]["path"] = str(data_root / "control-plane/opensandbox.db")
    profile_config["storage"]["allowed_host_paths"] = [str(data_root / "workspaces")]
    assert (
        prep_modules.config.validate_configuration(profile_config, data_root=data_root)
        == []
    )


@pytest.mark.parametrize(
    "data_root",
    [Path("relative/opensandbox"), Path("/"), Path("/var/lib/docker/opensandbox")],
)
def test_unsafe_data_root_is_rejected(prep_modules, data_root):
    with pytest.raises(ValueError):
        prep_modules.config.normalize_data_root(data_root)


def test_private_profile_generation_never_returns_the_api_key(
    tmp_path, prep_modules, monkeypatch
):
    private_root = tmp_path / "config/private"
    private_root.mkdir(parents=True)
    monkeypatch.setattr(prep_modules.configure, "PRIVATE_ROOT", private_root)
    monkeypatch.setattr(
        prep_modules.configure.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    output_directory = private_root / "opensandbox"
    profile_report = prep_modules.configure.create_private_profile(
        output_directory, Path("/mnt/data/opensandbox")
    )
    config_path = output_directory / "config.toml"
    env_path = output_directory / "server.env"
    secret_text = env_path.read_text(encoding="utf-8")
    api_key = secret_text.split("=", 1)[1].strip()
    assert profile_report["api_key"] == "generated_and_not_displayed"
    assert len(api_key) == 64
    assert api_key not in json.dumps(profile_report)
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert output_directory.stat().st_mode & 0o777 == 0o700
    assert "/mnt/data/opensandbox/workspaces" in config_path.read_text(encoding="utf-8")


def test_runtime_merge_preserves_existing_docker_settings(prep_modules):
    original_config = {
        "default-runtime": "runc",
        "registry-mirrors": ["https://mirror.example.invalid"],
        "runtimes": {"custom": {"path": "/usr/bin/custom-runtime"}},
    }
    merged_config = prep_modules.runtime.merge_runtime_policy(
        original_config, "/usr/bin/runsc"
    )
    assert merged_config["default-runtime"] == "runc"
    assert merged_config["registry-mirrors"] == original_config["registry-mirrors"]
    assert merged_config["runtimes"]["custom"] == {"path": "/usr/bin/custom-runtime"}
    assert merged_config["runtimes"]["runsc"] == {
        "path": "/usr/bin/runsc",
        "runtimeArgs": ["--platform=systrap", "--network=sandbox", "--overlay2=none"],
    }
    assert "runsc" not in original_config["runtimes"]


def test_runtime_merge_replaces_previous_host_network_policy(prep_modules, monkeypatch):
    monkeypatch.setattr(prep_modules.runtime.shutil, "which", lambda path: path)
    previous_config = {
        "runtimes": {
            "runsc": {
                "path": "/usr/bin/runsc",
                "runtimeArgs": [
                    "--platform=systrap",
                    "--network=host",
                    "--overlay2=none",
                ],
            }
        }
    }

    merged_config = prep_modules.runtime.merge_runtime_policy(
        previous_config, "/usr/bin/runsc"
    )

    assert merged_config["runtimes"]["runsc"]["runtimeArgs"] == [
        "--platform=systrap",
        "--network=sandbox",
        "--overlay2=none",
    ]


def test_runtime_merge_rejects_conflicting_existing_policy(prep_modules):
    conflicting_config = {"runtimes": {"runsc": {"runtimeArgs": ["--debug"]}}}
    with pytest.raises(prep_modules.runtime.InstallFailure):
        prep_modules.runtime.merge_runtime_policy(conflicting_config, "/usr/bin/runsc")
