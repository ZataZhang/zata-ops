"""``zata-ops deploy`` commands for inspection and rollback.

三条命令都基于 GitHub 的部署事实：``status`` 看当前版本与最近部署，``history``
列部署事件，``rollback`` 触发 ``workflow_dispatch`` 回滚到指定镜像 tag。默认输出
给人看，``--json`` 输出给 agent 消费。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer

from zata_ops.deploy.github import (
    GitHubCliError,
    latest_deployment_status,
    latest_workflow_run,
    list_deployments,
    trigger_workflow,
)
from zata_ops.deploy.health import probe_health
from zata_ops.deploy.targets import (
    DEFAULT_TARGETS_FILE,
    DeployTarget,
    DeployTargetError,
    resolve_deploy_target,
    validate_image_tag,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)

EXIT_QUERY_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_CONFIRMATION_REQUIRED = 3


@app.command("status")
def status_command(
    target_name: str = typer.Option(..., "--target", "-t", help="部署 target 名称。"),
    targets_file: Path = typer.Option(
        DEFAULT_TARGETS_FILE, "--targets-file", help="私有部署 target TOML 路径。"
    ),
    history_limit: int = typer.Option(5, "--limit", help="附带返回的最近部署条数。"),
    check_health: bool = typer.Option(
        False, "--check-health", help="额外请求 target 配置的健康检查地址。"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """查看目标环境当前版本、最近部署与工作流状态。

    Args:
        target_name: 部署 target 名称。
        targets_file: 私有 target 文件路径。
        history_limit: 最近部署条数。
        check_health: 是否发起健康检查请求。
        json_output: 是否输出 JSON。
    """
    try:
        target = resolve_deploy_target(target_name, targets_file)
    except DeployTargetError as exc:
        _fail(
            "deploy.status", "invalid_target", str(exc), json_output, EXIT_CONFIG_ERROR
        )

    try:
        deployment_summaries = _load_deployment_summaries(
            target.repo, target.environment, history_limit
        )
    except GitHubCliError as exc:
        _fail(
            "deploy.status",
            "github_query_failed",
            str(exc),
            json_output,
            EXIT_QUERY_FAILED,
        )

    try:
        workflow_run = latest_workflow_run(target.repo, target.workflow)
    except GitHubCliError as exc:
        _fail(
            "deploy.status",
            "github_query_failed",
            str(exc),
            json_output,
            EXIT_QUERY_FAILED,
        )

    health_result = None
    if check_health:
        health_result = _probe_target_health(target)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": "deploy.status",
        "checked_at": _now_iso(),
        "target": _target_payload(target),
        "current_deployment": deployment_summaries[0] if deployment_summaries else None,
        "recent_deployments": deployment_summaries,
        "latest_workflow_run": workflow_run or None,
        "health": health_result,
    }

    if json_output:
        _print_json(payload)
        return

    typer.echo(f"target: {target.name} ({target.environment})  repo: {target.repo}")
    current_deployment = payload["current_deployment"]
    if current_deployment:
        typer.echo(
            f"current: {current_deployment['image_release']}  "
            f"status={current_deployment['status']}  ref={current_deployment['ref']}"
        )
    else:
        typer.echo("current: 无部署记录")
    if workflow_run:
        typer.echo(
            f"latest run: {workflow_run.get('status')}/{workflow_run.get('conclusion') or '-'}"
            f"  {workflow_run.get('url', '')}"
        )
    if health_result is not None:
        health_state = "ok" if health_result.get("ok") else "failed"
        typer.echo(
            f"health: {health_state}  status={health_result.get('status')}  "
            f"{health_result.get('latency_ms')}ms"
        )


@app.command("history")
def history_command(
    target_name: str = typer.Option(..., "--target", "-t", help="部署 target 名称。"),
    targets_file: Path = typer.Option(DEFAULT_TARGETS_FILE, "--targets-file"),
    limit: int = typer.Option(10, "--limit", help="返回条数上限。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """列出目标环境最近的部署事件。

    Args:
        target_name: 部署 target 名称。
        targets_file: 私有 target 文件路径。
        limit: 返回条数上限。
        json_output: 是否输出 JSON。
    """
    try:
        target = resolve_deploy_target(target_name, targets_file)
    except DeployTargetError as exc:
        _fail(
            "deploy.history", "invalid_target", str(exc), json_output, EXIT_CONFIG_ERROR
        )

    try:
        deployment_summaries = _load_deployment_summaries(
            target.repo, target.environment, limit
        )
    except GitHubCliError as exc:
        _fail(
            "deploy.history",
            "github_query_failed",
            str(exc),
            json_output,
            EXIT_QUERY_FAILED,
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": "deploy.history",
        "checked_at": _now_iso(),
        "target": _target_payload(target),
        "limit": limit,
        "deployments": deployment_summaries,
    }

    if json_output:
        _print_json(payload)
        return

    if not deployment_summaries:
        typer.echo("没有部署记录")
        return

    typer.echo(f"{'created_at':<26} {'release':<28} {'status':<12} ref")
    for deployment in deployment_summaries:
        typer.echo(
            f"{(deployment['created_at'] or '-'):<26} "
            f"{deployment['image_release']:<28} "
            f"{(deployment['status'] or '-'):<12} "
            f"{deployment['ref'] or '-'}"
        )


@app.command("rollback")
def rollback_command(
    target_name: str = typer.Option(..., "--target", "-t", help="部署 target 名称。"),
    image_tag: str = typer.Option(
        ..., "--to", help="目标镜像 tag，例如 <sha>-production。"
    ),
    targets_file: Path = typer.Option(DEFAULT_TARGETS_FILE, "--targets-file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印回滚计划，不触发。"),
    confirm: bool = typer.Option(False, "--confirm", help="确认执行回滚。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON。"),
) -> None:
    """回滚到指定镜像 tag（触发 workflow_dispatch，不重新构建）。

    Args:
        target_name: 部署 target 名称。
        image_tag: 目标镜像 tag。
        targets_file: 私有 target 文件路径。
        dry_run: 只输出计划。
        confirm: 确认执行。
        json_output: 是否输出 JSON。
    """
    try:
        target = resolve_deploy_target(target_name, targets_file)
        validated_tag = validate_image_tag(image_tag)
    except DeployTargetError as exc:
        _fail(
            "deploy.rollback",
            "invalid_request",
            str(exc),
            json_output,
            EXIT_CONFIG_ERROR,
        )

    rollback_inputs = {
        "target_environment": target.environment,
        "rollback_tag": validated_tag,
    }
    planned_command = _rollback_command(target, validated_tag, rollback_inputs)
    plan: dict[str, Any] = {
        "schema_version": 1,
        "operation": "deploy.rollback",
        "checked_at": _now_iso(),
        "target": _target_payload(target),
        "rollback_tag": validated_tag,
        "command": planned_command,
    }

    if dry_run:
        plan["status"] = "dry_run"
        _emit_plan(plan, json_output)
        return

    if not confirm:
        plan["status"] = "confirmation_required"
        plan["hint"] = "确认后加 --confirm 再执行；生产环境请先核对目标 tag 与变更范围"
        _emit_plan(plan, json_output)
        raise typer.Exit(code=EXIT_CONFIRMATION_REQUIRED)

    try:
        trigger_result = trigger_workflow(
            target.repo,
            target.workflow,
            target.dispatch_ref,
            rollback_inputs,
        )
    except GitHubCliError as exc:
        _fail(
            "deploy.rollback",
            "github_dispatch_failed",
            str(exc),
            json_output,
            EXIT_QUERY_FAILED,
        )

    if trigger_result.exit_code != 0:
        _fail(
            "deploy.rollback",
            "github_dispatch_failed",
            trigger_result.stderr.strip()[:400] or "gh workflow run 失败",
            json_output,
            EXIT_QUERY_FAILED,
        )

    plan["status"] = "triggered"
    plan["workflow_run"] = _safe_latest_run(target)
    _emit_plan(plan, json_output)


_RUN_ID_PATTERN = re.compile(r"/actions/runs/(\d+)")


def _load_deployment_summaries(
    repo: str, environment: str, limit: int
) -> list[dict[str, Any]]:
    """拉取部署记录、按版本去重后截断。

    每次 workflow 里多个 job 都声明了 environment，因此同一版本会产生多条
    deployment 记录（构建 job + 部署 job）。这里先按 ref + sha 取每个版本的第一条
    记录，每条只查一次状态，避免为重复记录反复调用 GitHub。

    Args:
        repo: owner/name。
        environment: GitHub Environment 名称。
        limit: 去重后返回条数。

    Returns:
        list: 部署摘要列表，最新在前。
    """
    fetch_limit = max(limit * 4, 10)
    deployment_records = list_deployments(repo, environment, fetch_limit)
    unique_records = _first_record_per_version(deployment_records)[:limit]
    return [_summarize_deployment(repo, record) for record in unique_records]


def _first_record_per_version(
    deployment_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 ref + sha 去重，保留最新的一条原始记录。"""
    seen_versions: set[tuple[str, str]] = set()
    unique_records: list[dict[str, Any]] = []
    for record in deployment_records:
        version_key = (str(record.get("ref") or ""), str(record.get("sha") or ""))
        if version_key in seen_versions:
            continue
        seen_versions.add(version_key)
        unique_records.append(record)
    return unique_records


def _summarize_deployment(
    repo: str, deployment_record: dict[str, Any]
) -> dict[str, Any]:
    """把 deployment 记录与最新状态合并成稳定结构。

    Args:
        repo: owner/name。
        deployment_record: GitHub deployment 记录。

    Returns:
        dict: 供人和 agent 共用的部署摘要。
    """
    deployment_id = deployment_record.get("id")
    status_payload: dict[str, Any] = {}
    if isinstance(deployment_id, int):
        try:
            status_payload = latest_deployment_status(repo, deployment_id)
        except GitHubCliError:
            # 单条状态查询失败不应让整个列表失败
            status_payload = {}

    deployment_sha = deployment_record.get("sha") or ""
    environment = deployment_record.get("environment") or ""
    status_log_url = status_payload.get("log_url") or ""
    run_id_match = _RUN_ID_PATTERN.search(status_log_url)
    return {
        "id": deployment_id,
        "environment": environment,
        "ref": deployment_record.get("ref"),
        "sha": deployment_sha,
        "image_release": f"{deployment_sha}-{environment}" if deployment_sha else "",
        "created_at": deployment_record.get("created_at"),
        "status": status_payload.get("state") or "unknown",
        "status_description": status_payload.get("description"),
        "creator": (deployment_record.get("creator") or {}).get("login"),
        "run_id": int(run_id_match.group(1)) if run_id_match else None,
        "log_url": status_log_url or None,
    }


def _probe_target_health(target: DeployTarget) -> dict[str, Any]:
    """对 target 配置的健康地址做一次探测。"""
    if not target.health_url:
        return {
            "ok": False,
            "status": None,
            "error": "target 未配置 health_url",
        }
    return probe_health(target.health_url)


def _target_payload(target: DeployTarget) -> dict[str, Any]:
    """输出可安全暴露的 target 元数据。"""
    return {
        "name": target.name,
        "repo": target.repo,
        "workflow": target.workflow,
        "environment": target.environment,
        "production": target.production,
    }


def _rollback_command(
    target: DeployTarget,
    image_tag: str,
    inputs: dict[str, str],
) -> list[str]:
    """构造回滚命令，供 dry-run 展示与人工复核。"""
    command = [
        "gh",
        "workflow",
        "run",
        target.workflow,
        "--repo",
        target.repo,
        "--ref",
        target.dispatch_ref,
    ]
    for input_key, input_value in inputs.items():
        command.extend(["-f", f"{input_key}={input_value}"])
    return command


def _safe_latest_run(target: DeployTarget) -> Optional[dict[str, Any]]:
    """触发后尽力取回最近一次 run，失败不影响回滚结果。"""
    try:
        return latest_workflow_run(target.repo, target.workflow) or None
    except GitHubCliError:
        return None


def _emit_plan(plan: dict[str, Any], json_output: bool) -> None:
    """输出回滚计划（JSON 或人读文本）。"""
    if json_output:
        _print_json(plan)
        return

    typer.echo(f"target: {plan['target']['name']} ({plan['target']['environment']})")
    typer.echo(f"rollback tag: {plan['rollback_tag']}")
    typer.echo(f"status: {plan['status']}")
    typer.echo(f"command: {' '.join(plan['command'])}")
    if plan.get("hint"):
        typer.echo(f"hint: {plan['hint']}")
    workflow_run = plan.get("workflow_run")
    if workflow_run:
        typer.echo(f"run: {workflow_run.get('url', '')}")


def _fail(
    operation: str,
    reason: str,
    message: str,
    json_output: bool,
    exit_code: int,
) -> None:
    """输出统一的错误结构并退出。"""
    payload = {
        "schema_version": 1,
        "operation": operation,
        "status": "error",
        "reason": reason,
        "message": message,
    }
    if json_output:
        _print_json(payload)
    else:
        typer.echo(f"{operation} failed: {reason}: {message}", err=True)
    raise typer.Exit(code=exit_code)


def _print_json(payload: dict[str, Any]) -> None:
    """统一 JSON 输出格式。"""
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _now_iso() -> str:
    """返回带时区的当前时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
