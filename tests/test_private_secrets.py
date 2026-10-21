"""Exercise credential storage and subprocess isolation with synthetic values."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/diagnostics/private_secrets.py"


@pytest.fixture
def credential_dir():
    """Allocate an ignored fixture directory without touching real credentials."""
    private_root = REPO_ROOT / "config/private"
    private_root.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(
        prefix="test-secrets-", dir=private_root
    ) as fixture_dir:
        yield Path(fixture_dir)


def _run(credential_dir: Path, arguments: list[str], *, input_text: str | None = None):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--private-dir",
            str(credential_dir),
            *arguments,
        ],
        input=input_text,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )


def test_update_roundtrip_and_backup(credential_dir):
    first_value = "synthetic-'quote'-\\slash-${UNCHANGED}-中文"
    assert (
        _run(
            credential_dir, ["set", "TEST_TOKEN", "--stdin"], input_text=first_value
        ).returncode
        == 0
    )
    secret_path = credential_dir / "secrets.env"
    assert dotenv_values(secret_path, interpolate=False)["TEST_TOKEN"] == first_value
    assert (
        _run(
            credential_dir, ["set", "OTHER_TOKEN", "--stdin"], input_text="other"
        ).returncode
        == 0
    )
    updated = _run(
        credential_dir, ["set", "TEST_TOKEN", "--stdin"], input_text="replacement"
    )
    assert updated.returncode == 0
    assert first_value not in updated.stdout + updated.stderr
    assert dotenv_values(secret_path, interpolate=False) == {
        "TEST_TOKEN": "replacement",
        "OTHER_TOKEN": "other",
    }
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert credential_dir.stat().st_mode & 0o777 == 0o700
    archive_path = sorted((credential_dir / "backups").glob("*.tar.gz"))[-1]
    with tarfile.open(archive_path) as archive:
        restored_text = archive.extractfile("secrets.env").read().decode("utf-8")
        assert (
            dotenv_values(stream=StringIO(restored_text), interpolate=False)[
                "TEST_TOKEN"
            ]
            == first_value
        )
    listed = _run(credential_dir, ["list"])
    assert listed.stdout.splitlines() == ["OTHER_TOKEN", "TEST_TOKEN"]


def test_run_injects_only_requested_values_and_hides_output(credential_dir):
    token_value = "synthetic-never-printed"
    _run(credential_dir, ["set", "TEST_TOKEN", "--stdin"], input_text=token_value)
    _run(credential_dir, ["set", "OTHER_TOKEN", "--stdin"], input_text="other")
    child_script = (
        "import os,sys; "
        "assert os.environ['TEST_TOKEN']=='synthetic-never-printed'; "
        "assert 'OTHER_TOKEN' not in os.environ; "
        "print(os.environ['TEST_TOKEN']); "
        "print(os.environ['TEST_TOKEN'],file=sys.stderr); sys.exit(7)"
    )
    executed = _run(
        credential_dir,
        ["run", "--env", "TEST_TOKEN", "--", sys.executable, "-c", child_script],
    )
    assert executed.returncode == 7
    assert token_value not in executed.stdout + executed.stderr
    assert "exit=7" in executed.stdout


def test_invalid_input_and_noninteractive_prompt_do_not_write(credential_dir):
    for invalid_value in ("", "two\nlines", "null\x00byte"):
        assert (
            _run(
                credential_dir,
                ["set", "TEST_TOKEN", "--stdin"],
                input_text=invalid_value,
            ).returncode
            == 1
        )
    assert (
        _run(
            credential_dir, ["set", "bad-name", "--stdin"], input_text="synthetic"
        ).returncode
        == 1
    )
    assert (
        _run(credential_dir, ["set", "TEST_TOKEN"], input_text="synthetic").returncode
        == 1
    )
    assert not (credential_dir / "secrets.env").exists()


def test_missing_reference_does_not_launch_command(credential_dir):
    marker_path = credential_dir / "executed"
    executed = _run(
        credential_dir,
        [
            "run",
            "--env",
            "MISSING_TOKEN",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker_path)!r}).touch()",
        ],
    )
    assert executed.returncode == 1
    assert not marker_path.exists()


def test_refuse_public_directory_and_symlink(credential_dir, tmp_path):
    assert (
        _run(
            tmp_path, ["set", "TEST_TOKEN", "--stdin"], input_text="synthetic"
        ).returncode
        == 1
    )
    external_path = tmp_path / "untouched.env"
    external_path.write_text("unchanged", encoding="utf-8")
    (credential_dir / "secrets.env").symlink_to(external_path)
    assert (
        _run(
            credential_dir, ["set", "TEST_TOKEN", "--stdin"], input_text="synthetic"
        ).returncode
        == 1
    )
    assert external_path.read_text(encoding="utf-8") == "unchanged"
