"""Pytest configuration for zata-ops.

Adds the ``src/`` directory to ``sys.path`` so ``import zata_ops`` resolves
without an editable install when running from a fresh worktree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _ensure_src_on_path() -> None:
    """Insert ``src/`` into ``sys.path`` for local imports."""
    project_root_path = Path(__file__).resolve().parents[1]
    src_path = project_root_path / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


_ensure_src_on_path()


@pytest.fixture(scope="session")
def backup_test_identity(tmp_path_factory):
    """Create an isolated SSH key for real backup encryption tests."""
    if shutil.which("age") is None:
        pytest.skip("age executable is required for backup integration tests")
    identity_path = tmp_path_factory.mktemp("backup-identity") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity_path)],
        check=True,
        capture_output=True,
    )
    return identity_path


@pytest.fixture(autouse=True)
def isolate_backup_identity(request, monkeypatch):
    """Keep backup tests independent of the developer's real SSH identity."""
    if request.module.__name__ in {
        "test_private_backup_encryption",
        "test_private_secrets",
        "test_inventory_format",
    }:
        identity_path = request.getfixturevalue("backup_test_identity")
        monkeypatch.setenv("ZATA_BACKUP_IDENTITY", str(identity_path))
        monkeypatch.setenv("ZATA_BACKUP_RECIPIENT", str(identity_path) + ".pub")
