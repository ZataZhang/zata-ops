"""Opt-in end-to-end checks using disposable OpenBao and Object Lock containers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

pytestmark = pytest.mark.skipif(
    os.environ.get("ZATA_OPS_TEST_OPENBAO") != "1",
    reason="set ZATA_OPS_TEST_OPENBAO=1 to start isolated Docker containers",
)

_REPO = Path(__file__).resolve().parents[1]
_PROCESS_ENV = {
    key: os.environ[key]
    for key in ("PATH", "HOME", "SYSTEMROOT", "TEMP")
    if key in os.environ
}
_ENV = {
    "AWS_ACCESS_KEY_ID": "fixture-audit",
    "AWS_SECRET_ACCESS_KEY": "fixture-storage-only",
}
_BAO_IMAGE = "openbao/openbao:2.7.2"
_STORAGE_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"


def _run(*arguments, env=None, check=True):
    return subprocess.run(
        arguments,
        env=_PROCESS_ENV if env is None else env,
        check=check,
        text=True,
        capture_output=True,
        cwd=_REPO,
    )


def _wait(probe):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (OSError, BotoCoreError, ClientError, subprocess.CalledProcessError):
            pass
        time.sleep(0.25)
    pytest.fail("disposable service did not become ready")


@pytest.fixture
def live_stack(tmp_path):
    _run(
        "openssl",
        "req",
        "-x509",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout",
        str(tmp_path / "private.key"),
        "-out",
        str(tmp_path / "public.crt"),
    )
    (tmp_path / "bao.hcl").write_text(
        """
disable_mlock = true
api_addr = "https://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"
storage "raft" {
  path = "/fixture/raft"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_cert_file = "/fixture/public.crt"
  tls_key_file = "/fixture/private.key"
}
audit "file" "fixture" {
  options {
    file_path = "/fixture/audit.jsonl"
    log_raw = "false"
  }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "raft").mkdir()
    (tmp_path / "objects").mkdir()
    containers = []
    try:
        for image, port, arguments in (
            (
                _BAO_IMAGE,
                8200,
                (
                    "--user",
                    "0",
                    "--entrypoint",
                    "bao",
                    _BAO_IMAGE,
                    "server",
                    "-config=/fixture/bao.hcl",
                ),
            ),
            (
                _STORAGE_IMAGE,
                9000,
                (
                    "-e",
                    f"MINIO_ROOT_USER={_ENV['AWS_ACCESS_KEY_ID']}",
                    "-e",
                    f"MINIO_ROOT_PASSWORD={_ENV['AWS_SECRET_ACCESS_KEY']}",
                    _STORAGE_IMAGE,
                    "server",
                    "/fixture/objects",
                    "--certs-dir",
                    "/fixture",
                ),
            ),
        ):
            container = _run(
                "docker",
                "run",
                "-d",
                "-v",
                f"{tmp_path}:/fixture",
                "-p",
                f"127.0.0.1::{port}",
                *arguments,
            ).stdout.strip()
            containers.append(container)
        bao_container, object_container = containers
        bao_port = (
            _run("docker", "port", bao_container, "8200/tcp")
            .stdout.strip()
            .split(":")[-1]
        )
        object_port = (
            _run("docker", "port", object_container, "9000/tcp")
            .stdout.strip()
            .split(":")[-1]
        )

        def bao(*arguments, token=""):
            return _run(
                "docker",
                "exec",
                "-e",
                "BAO_ADDR=https://127.0.0.1:8200",
                "-e",
                "BAO_CACERT=/fixture/public.crt",
                "-e",
                f"BAO_TOKEN={token}",
                bao_container,
                "bao",
                *arguments,
            ).stdout

        initialization = _wait(
            lambda: bao(
                "operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json"
            )
        )
        initialized = json.loads(initialization)
        bao("operator", "unseal", initialized["unseal_keys_b64"][0])
        storage_url = f"https://127.0.0.1:{object_port}"
        storage = boto3.client(
            "s3",
            endpoint_url=storage_url,
            verify=str(tmp_path / "public.crt"),
            aws_access_key_id=_ENV["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=_ENV["AWS_SECRET_ACCESS_KEY"],
            region_name="us-east-1",
            config=Config(retries={"max_attempts": 0}, s3={"addressing_style": "path"}),
        )
        _wait(lambda: storage.list_buckets())

        def stop_auditing():
            _run(
                "docker",
                "exec",
                bao_container,
                "mv",
                "/fixture/audit.jsonl",
                "/fixture/audit.jsonl.old",
            )
            _run(
                "docker",
                "exec",
                bao_container,
                "ln",
                "-s",
                "/dev/full",
                "/fixture/audit.jsonl",
            )
            _run("docker", "kill", "--signal=HUP", bao_container)

        yield (
            bao,
            initialized["root_token"],
            f"https://127.0.0.1:{bao_port}",
            storage,
            storage_url,
            stop_auditing,
        )
    finally:
        for container in reversed(containers):
            _run("docker", "rm", "-f", container, check=False)


def test_openbao_policy_and_immutable_audit(tmp_path, live_stack):
    """Exercise installed CLI, exact-path ACL, checkpoint retry and Object Lock."""
    bao, root, bao_url, storage, storage_url, stop_auditing = live_stack
    for mount in ("fixture-config", "fixture-secrets"):
        bao("secrets", "enable", f"-path={mount}", "kv-v2", token=root)
    for path, value in (
        ("first", "fixture-first-unique"),
        ("second", "fixture-second-unique"),
    ):
        bao(
            "kv",
            "put",
            "-mount=fixture-secrets",
            f"dev/{path}",
            f"value={value}",
            token=root,
        )
    policy_prefix = (
        'path "fixture-config/data/dev/registry" { capabilities = ["read"] }\n'
    )
    for role, paths in (("limited", ["first"]), ("runner", ["first", "second"])):
        policy = policy_prefix + "\n".join(
            f'path "fixture-secrets/data/dev/{path}" {{ capabilities = ["read"] }}'
            for path in paths
        )
        (tmp_path / f"{role}.hcl").write_text(policy, encoding="utf-8")
        bao("policy", "write", role, f"/fixture/{role}.hcl", token=root)

    def token_for(role):
        issued = bao(
            "token", "create", f"-policy={role}", "-ttl=5m", "-format=json", token=root
        )
        return json.loads(issued)["auth"]["client_token"]

    profile = tmp_path / "profile.toml"
    profile.write_text(
        f'address = "{bao_url}"\nca_file = "public.crt"\nenvironment = "dev"\n'
        'inventory_mount = "fixture-config"\ncredentials_mount = "fixture-secrets"\n',
        encoding="utf-8",
    )
    (tmp_path / "servers.toml").write_text(
        (_REPO / "config/servers.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "deploy_targets.toml").write_text(
        '[[targets]]\nname="example-app"\nrepo="example/demo"\nworkflow="cd.yml"\nenvironment="development"\n',
        encoding="utf-8",
    )
    (tmp_path / "services.toml").write_text(
        "\n".join(
            f'[[services]]\nname="{name}"\nserver=""\ntype="other"\nhost=""\nport=""\n'
            f'database=""\nusername=""\nnotes=""\ncredential_ref="{name}"\n'
            for name in ("first", "second")
        ),
        encoding="utf-8",
    )

    def cli(session, *arguments, check=True):
        return _run(
            sys.executable,
            "-m",
            "zata_ops",
            "--profile",
            str(profile),
            *arguments,
            env={
                **_PROCESS_ENV,
                "BAO_TOKEN": session,
                "UNRELATED_CREDENTIAL": "fixture-do-not-pass",
            },
            check=check,
        )

    publish = (
        "config",
        "publish",
        "--from-dir",
        str(tmp_path),
        "--apply",
        "--expected-version",
        "0",
    )
    assert json.loads(cli(root, *publish).stdout)["published_version"] == 1
    assert cli(root, *publish, check=False).returncode == 2
    reader = token_for("runner")
    assert cli(reader, *publish[:-1], "1", check=False).returncode == 2
    app_plan = json.loads(
        cli(reader, "app", "inspect", "example-app", "--dry-run").stdout
    )
    assert app_plan["status"] == "planned"
    assert app_plan["inventory"]["version"] == 1
    service_preview = json.loads(cli(reader, "service", "show", "first").stdout)
    assert service_preview["inventory_version"] == 1
    plan = cli(
        reader,
        "deploy",
        "rollback",
        "--target",
        "example-app",
        "--to",
        "example",
        "--dry-run",
        "--json",
    )
    assert json.loads(plan.stdout)["inventory_version"] == 1
    service_file = tmp_path / "services.toml"
    service_file.write_text(
        service_file.read_text(encoding="utf-8").replace('notes=""', 'notes="revised"'),
        encoding="utf-8",
    )
    assert json.loads(cli(root, *publish[:-1], "1").stdout)["published_version"] == 2
    rollback = cli(
        root,
        "config",
        "rollback",
        "--to-version",
        "1",
        "--apply",
        "--expected-version",
        "2",
    )
    assert json.loads(rollback.stdout)["published_version"] == 3
    rolled_back = json.loads(cli(reader, "service", "show", "first").stdout)
    assert rolled_back["inventory_version"] == 3
    assert (
        rolled_back["services"][0]["notes"] == service_preview["services"][0]["notes"]
    )
    marker = tmp_path / "child-started"
    command = (
        "service",
        "run",
        "--credential",
        "first:FIRST_TOKEN",
        "--credential",
        "second:SECOND_TOKEN",
        "--",
        sys.executable,
        "-c",
        "import os,sys; from pathlib import Path; Path(sys.argv[1]).touch(); "
        "assert os.environ['FIRST_TOKEN']=='fixture-first-unique'; "
        "assert os.environ['SECOND_TOKEN']=='fixture-second-unique'; "
        "assert not {'BAO_TOKEN','UNRELATED_CREDENTIAL'} & os.environ.keys()",
        str(marker),
    )
    assert cli(token_for("limited"), *command, check=False).returncode == 1
    assert not marker.exists()
    assert cli(reader, *command).returncode == 0
    assert marker.exists()
    audit_path = tmp_path / "audit.jsonl"
    raw_audit = audit_path.read_text(encoding="utf-8")
    assert "fixture-first-unique" not in raw_audit
    assert "fixture-second-unique" not in raw_audit
    records = [json.loads(line) for line in raw_audit.splitlines()]
    responses = [
        record
        for record in records
        if record["type"] == "response"
        and record.get("request", {}).get("path") == "fixture-secrets/data/dev/second"
        and record["request"].get("operation") == "read"
    ]
    assert len(responses) == 2

    config = tmp_path / "archive.toml"
    config.write_text(
        f'endpoint_url="{storage_url}"\nca_file="public.crt"\nregion="us-east-1"\npath_style=true\n'
        'source="audit.jsonl"\ncheckpoint="checkpoint.sqlite"\nbucket="fixture-audit"\nprefix="events"\nretention_days=1\ncredential_mount="fixture-secrets"\n',
        encoding="utf-8",
    )

    def archive(*, check=True):
        return _run(
            sys.executable,
            "-m",
            "zata_ops",
            "audit",
            "archive",
            "--config",
            str(config),
            env={**_PROCESS_ENV, **_ENV},
            check=check,
        )

    assert archive(check=False).returncode == 1
    storage.create_bucket(Bucket="fixture-audit", ObjectLockEnabledForBucket=True)
    storage.put_object_lock_configuration(
        Bucket="fixture-audit",
        ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 2}},
        },
    )
    monitored = []
    for _ in range(5):
        backlog = json.loads(archive().stdout)
        monitored.extend(backlog["access_events"])
        if not backlog["pending_bytes"]:
            break
    assert backlog["pending_bytes"] == 0
    assert {entry["audit_request_id"] for entry in monitored} >= {
        response["request"]["id"] for response in responses
    }
    assert {entry["outcome"] for entry in monitored} == {"ok", "error"}
    expected_prefix = f"events/{responses[-1]['request']['id']}/response-"
    versions = storage.list_object_versions(
        Bucket="fixture-audit", Prefix=expected_prefix
    )["Versions"]
    assert len(versions) == 1
    locator = {
        "Bucket": "fixture-audit",
        "Key": versions[0]["Key"],
        "VersionId": versions[0]["VersionId"],
    }
    with pytest.raises(ClientError):
        storage.delete_object(**locator)
    with pytest.raises(ClientError):
        storage.put_object_retention(
            **locator,
            Retention={
                "Mode": "COMPLIANCE",
                "RetainUntilDate": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        )
    recovered = storage.get_object(**locator)["Body"].read()
    assert json.loads(recovered) == responses[-1]
    assert json.loads(archive().stdout)["archived_events"] == 0

    # Replay the source from a replacement checkpoint. Locked object keys are idempotent.
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "checkpoint.sqlite", "replay.sqlite"
        ),
        encoding="utf-8",
    )
    for _ in range(5):
        if not json.loads(archive().stdout)["pending_bytes"]:
            break
    replayed_versions = storage.list_object_versions(
        Bucket="fixture-audit", Prefix=expected_prefix
    )
    assert len(replayed_versions["Versions"]) == 1

    stop_auditing()
    marker.unlink()

    def denied_when_auditing_fails():
        attempt = cli(reader, *command, check=False)
        if attempt.returncode:
            return True
        marker.unlink()
        return False

    assert _wait(denied_when_auditing_fails)
    assert not marker.exists()


def test_isolated_raft_restore_reconciles_revoked_session(live_stack):
    """Restore a real snapshot, then reapply revocation before reopening access."""
    bao, root, _, _, _, _ = live_stack
    bao("secrets", "enable", "-path=fixture-recovery", "kv-v2", token=root)
    bao(
        "kv",
        "put",
        "-mount=fixture-recovery",
        "dev/service",
        "value=fixture-old",
        token=root,
    )
    old_session = json.loads(
        bao("token", "create", "-ttl=5m", "-format=json", token=root)
    )["auth"]["client_token"]
    bao("operator", "raft", "snapshot", "save", "/fixture/fixture.snap", token=root)
    bao("token", "revoke", old_session, token=root)
    bao(
        "kv",
        "put",
        "-mount=fixture-recovery",
        "dev/service",
        "value=fixture-current",
        token=root,
    )
    with pytest.raises(subprocess.CalledProcessError):
        bao("token", "lookup", token=old_session)

    bao("operator", "raft", "snapshot", "restore", "/fixture/fixture.snap", token=root)
    _wait(lambda: bao("token", "lookup", token=root))
    assert (
        bao(
            "kv",
            "get",
            "-mount=fixture-recovery",
            "-field=value",
            "dev/service",
            token=root,
        ).strip()
        == "fixture-old"
    )
    assert bao("token", "lookup", token=old_session)
    bao("token", "revoke", old_session, token=root)
    bao(
        "kv",
        "put",
        "-mount=fixture-recovery",
        "dev/service",
        "value=fixture-current",
        token=root,
    )
    new_session = json.loads(
        bao("token", "create", "-ttl=5m", "-format=json", token=root)
    )["auth"]["client_token"]
    assert (
        bao(
            "kv",
            "get",
            "-mount=fixture-recovery",
            "-field=value",
            "dev/service",
            token=new_session,
        ).strip()
        == "fixture-current"
    )
    with pytest.raises(subprocess.CalledProcessError):
        bao("token", "lookup", token=old_session)
