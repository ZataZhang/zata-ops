"""Tests for ``zata-ops deploy`` status, history and rollback commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import zata_ops.deploy.cli as deploy_cli
from typer.testing import CliRunner
from zata_ops.cli import app
from zata_ops.deploy.github import CommandResult
from zata_ops.deploy.targets import DeployTargetError, load_deploy_targets

ROLLBACK_SHA = "c" * 40
ROLLBACK_TAG = f"{ROLLBACK_SHA}-production"

DEPLOYMENTS = [
    {
        "id": 11,
        "environment": "production",
        "ref": "v0.5.36",
        "sha": "a" * 40,
        "created_at": "2026-01-01T10:00:00Z",
        "creator": {"login": "operator"},
    },
    {
        "id": 10,
        "environment": "production",
        "ref": "v0.5.35",
        "sha": "b" * 40,
        "created_at": "2025-12-30T10:00:00Z",
        "creator": {"login": "operator"},
    },
]

TARGETS_TEMPLATE = """[[targets]]
name = "prod"
repo = "example-org/example-repo"
workflow = "cd.yml"
environment = "production"
dispatch_ref = "main"
health_url = "https://example.invalid/health"
production = true
"""


def _write_targets_file(targets_path: Path) -> Path:
    """写入一份只用于测试的 target 配置。"""
    targets_path.write_text(TARGETS_TEMPLATE, encoding="utf-8")
    return targets_path


@pytest.fixture()
def targets_path(tmp_path: Path) -> Path:
    """提供临时 target 文件。"""
    return _write_targets_file(tmp_path / "deploy_targets.toml")


def _invoke(arguments: list[str], targets_path: Path):
    """通过顶层 CLI 调用 deploy 子命令。"""
    return CliRunner().invoke(
        app,
        ["deploy", *arguments, "--targets-file", str(targets_path)],
    )


def test_load_targets_rejects_duplicate_names(tmp_path: Path) -> None:
    """重复 target 名称必须报配置错误。"""
    duplicate_file = tmp_path / "duplicates.toml"
    duplicate_file.write_text(
        """[[targets]]
name = "prod"
repo = "example-org/example-repo"
workflow = "cd.yml"
environment = "production"

[[targets]]
name = "prod"
repo = "example-org/other-repo"
workflow = "cd.yml"
environment = "production"
""",
        encoding="utf-8",
    )

    with pytest.raises(DeployTargetError):
        load_deploy_targets(duplicate_file)


def test_status_json_reports_current_deployment_and_run(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """status --json 输出当前版本、最近部署与 workflow run。"""
    monkeypatch.setattr(
        deploy_cli,
        "list_deployments",
        lambda repo, environment, limit: DEPLOYMENTS[:limit],
    )
    monkeypatch.setattr(
        deploy_cli,
        "latest_deployment_status",
        lambda repo, deployment_id: {"state": "success", "description": "deployed"},
    )
    monkeypatch.setattr(
        deploy_cli,
        "latest_workflow_run",
        lambda repo, workflow: {
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/example-org/example-repo/actions/runs/1",
        },
    )

    result = _invoke(
        ["status", "--target", "prod", "--limit", "2", "--json"], targets_path
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["operation"] == "deploy.status"
    assert payload["target"]["environment"] == "production"
    assert payload["current_deployment"]["sha"] == "a" * 40
    assert payload["current_deployment"]["image_release"] == "a" * 40 + "-production"
    assert payload["current_deployment"]["status"] == "success"
    assert len(payload["recent_deployments"]) == 2
    assert payload["latest_workflow_run"]["conclusion"] == "success"


def test_status_check_health_includes_probe_result(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """--check-health 会带上健康检查结果。"""
    monkeypatch.setattr(deploy_cli, "list_deployments", lambda *args: [])
    monkeypatch.setattr(deploy_cli, "latest_workflow_run", lambda *args: {})
    monkeypatch.setattr(
        deploy_cli,
        "probe_health",
        lambda url: {"ok": True, "status": 200, "latency_ms": 12},
    )

    result = _invoke(
        ["status", "--target", "prod", "--check-health", "--json"], targets_path
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["health"]["ok"] is True
    assert payload["current_deployment"] is None


def test_status_dedupes_duplicate_deployment_records(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """同一版本的多条 deployment 记录（构建 job + 部署 job）只保留一条。"""
    duplicate_records = [
        dict(DEPLOYMENTS[0], id=21),
        dict(DEPLOYMENTS[0], id=22),
    ]
    queried_ids: list[int] = []

    def fake_status(repo: str, deployment_id: int):
        queried_ids.append(deployment_id)
        return {
            "state": "success",
            "log_url": "https://github.com/x/y/actions/runs/99/job/7",
        }

    monkeypatch.setattr(
        deploy_cli,
        "list_deployments",
        lambda repo, environment, limit: duplicate_records,
    )
    monkeypatch.setattr(deploy_cli, "latest_deployment_status", fake_status)
    monkeypatch.setattr(deploy_cli, "latest_workflow_run", lambda *args: {})

    result = _invoke(["status", "--target", "prod", "--json"], targets_path)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["recent_deployments"]) == 1
    assert queried_ids == [21]
    assert payload["current_deployment"]["status"] == "success"
    assert payload["current_deployment"]["run_id"] == 99


def test_status_unknown_target_exits_with_config_error(targets_path: Path) -> None:
    """不存在的 target 返回退出码 2。"""
    result = _invoke(["status", "--target", "missing", "--json"], targets_path)

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason"] == "invalid_target"


def test_history_json_respects_limit(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """history 把 limit 透传给 GitHub 并保持返回顺序。"""
    requested_limits: list[int] = []

    def fake_list(repo: str, environment: str, limit: int):
        requested_limits.append(limit)
        return DEPLOYMENTS[:limit]

    monkeypatch.setattr(deploy_cli, "list_deployments", fake_list)
    monkeypatch.setattr(
        deploy_cli, "latest_deployment_status", lambda *args: {"state": "success"}
    )

    result = _invoke(
        ["history", "--target", "prod", "--limit", "1", "--json"], targets_path
    )

    assert result.exit_code == 0
    # 为了去重会多取原始记录，最终返回条数仍按 --limit 截断
    assert requested_limits and requested_limits[0] >= 1
    payload = json.loads(result.stdout)
    assert len(payload["deployments"]) == 1
    assert payload["deployments"][0]["ref"] == "v0.5.36"


def test_rollback_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """没有 --confirm 时只输出计划，退出码 3，不触发 workflow。"""
    trigger_calls: list[dict] = []
    monkeypatch.setattr(
        deploy_cli,
        "trigger_workflow",
        lambda *args: trigger_calls.append({"args": args}) or CommandResult(0, "", ""),
    )

    result = _invoke(
        ["rollback", "--target", "prod", "--to", "a" * 40 + "-production", "--json"],
        targets_path,
    )

    assert result.exit_code == 3
    assert trigger_calls == []
    payload = json.loads(result.stdout)
    assert payload["status"] == "confirmation_required"
    assert "gh" in payload["command"]


def test_rollback_dry_run_prints_command_without_triggering(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """--dry-run 输出命令，不调用 gh。"""
    trigger_calls: list[dict] = []
    monkeypatch.setattr(
        deploy_cli,
        "trigger_workflow",
        lambda *args: trigger_calls.append({"args": args}) or CommandResult(0, "", ""),
    )

    result = _invoke(
        [
            "rollback",
            "--target",
            "prod",
            "--to",
            "b" * 40 + "-production",
            "--dry-run",
            "--json",
        ],
        targets_path,
    )

    assert result.exit_code == 0
    assert trigger_calls == []
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["command"][:2] == ["gh", "workflow"]
    assert "rollback_tag=" in " ".join(payload["command"])


def test_rollback_confirm_triggers_workflow(
    monkeypatch: pytest.MonkeyPatch, targets_path: Path
) -> None:
    """--confirm 触发 workflow_dispatch 并返回 run 信息。"""
    captured_calls: list[dict] = []

    def fake_trigger(repo: str, workflow: str, dispatch_ref: str, inputs: dict):
        captured_calls.append(
            {
                "repo": repo,
                "workflow": workflow,
                "dispatch_ref": dispatch_ref,
                "inputs": inputs,
            }
        )
        return CommandResult(0, "", "")

    monkeypatch.setattr(deploy_cli, "trigger_workflow", fake_trigger)
    monkeypatch.setattr(
        deploy_cli,
        "latest_workflow_run",
        lambda *args: {
            "url": "https://github.com/example-org/example-repo/actions/runs/2"
        },
    )

    result = _invoke(
        [
            "rollback",
            "--target",
            "prod",
            "--to",
            ROLLBACK_TAG,
            "--confirm",
            "--json",
        ],
        targets_path,
    )

    assert result.exit_code == 0
    assert captured_calls == [
        {
            "repo": "example-org/example-repo",
            "workflow": "cd.yml",
            "dispatch_ref": "main",
            "inputs": {
                "target_environment": "production",
                "rollback_tag": ROLLBACK_TAG,
            },
        }
    ]
    payload = json.loads(result.stdout)
    assert payload["status"] == "triggered"
    assert payload["workflow_run"]["url"].endswith("/2")


def test_rollback_rejects_invalid_tag(targets_path: Path) -> None:
    """非法镜像 tag 返回退出码 2，避免拼接任意参数。"""
    result = _invoke(
        ["rollback", "--target", "prod", "--to", "bad tag; rm -rf", "--json"],
        targets_path,
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason"] == "invalid_request"
