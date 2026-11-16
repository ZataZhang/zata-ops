"""Thin, read-mostly wrapper around the GitHub CLI.

部署事实以 GitHub 为准：workflow run、deployment 记录和回滚触发都通过 ``gh``
完成，不在本地维护第二份部署状态。所有命令都返回结构化数据，便于 agent 消费。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence


class GitHubCliError(RuntimeError):
    """gh 不可用、超时或返回失败。"""


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令的执行结果。

    Attributes:
        exit_code: 退出码。
        stdout: 标准输出。
        stderr: 标准错误。
    """

    exit_code: int
    stdout: str
    stderr: str


def run_command(argv: Sequence[str], *, timeout_seconds: int = 60) -> CommandResult:
    """执行命令并捕获输出。

    Args:
        argv: 完整命令参数列表。
        timeout_seconds: 超时时间。

    Returns:
        CommandResult: 退出码与输出。

    Raises:
        GitHubCliError: 命令不存在或超时。
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubCliError(f"未找到命令: {argv[0]}，请先安装并登录 gh") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubCliError(
            f"命令超时（{timeout_seconds}s）: {' '.join(argv)}"
        ) from exc
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def run_gh_json(arguments: Sequence[str], *, timeout_seconds: int = 60) -> Any:
    """执行 gh 命令并把 stdout 解析为 JSON。

    Args:
        arguments: 传给 gh 的参数（不含 gh 本身）。
        timeout_seconds: 超时时间。

    Returns:
        Any: 解析后的 JSON。

    Raises:
        GitHubCliError: 命令失败或输出不是合法 JSON。
    """
    result = run_command(["gh", *arguments], timeout_seconds=timeout_seconds)
    if result.exit_code != 0:
        raise GitHubCliError(
            f"gh 调用失败（exit={result.exit_code}）: {result.stderr.strip()[:400]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubCliError("gh 输出不是合法 JSON") from exc


def list_deployments(repo: str, environment: str, limit: int) -> list[dict[str, Any]]:
    """列出某个 GitHub Environment 的部署记录（最新在前）。

    Args:
        repo: owner/name。
        environment: GitHub Environment 名称。
        limit: 返回条数上限。

    Returns:
        list: deployment 记录列表。
    """
    payload = run_gh_json(
        [
            "api",
            f"repos/{repo}/deployments?environment={environment}&per_page={limit}",
        ]
    )
    if not isinstance(payload, list):
        raise GitHubCliError("deployments 接口返回的不是列表")
    return payload


def latest_deployment_status(repo: str, deployment_id: int) -> dict[str, Any]:
    """读取一条 deployment 的最新状态。

    Args:
        repo: owner/name。
        deployment_id: deployment 数字 ID。

    Returns:
        dict: 最新状态记录；没有状态时返回空字典。
    """
    payload = run_gh_json(
        ["api", f"repos/{repo}/deployments/{deployment_id}/statuses?per_page=1"]
    )
    if isinstance(payload, list) and payload:
        first_status = payload[0]
        if isinstance(first_status, dict):
            return first_status
    return {}


def latest_workflow_run(repo: str, workflow: str) -> dict[str, Any]:
    """读取指定 workflow 最近一次 run。

    Args:
        repo: owner/name。
        workflow: workflow 文件名或 ID。

    Returns:
        dict: run 摘要；没有 run 时返回空字典。
    """
    payload = run_gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--limit",
            "1",
            "--json",
            "databaseId,status,conclusion,headBranch,headSha,displayTitle,createdAt,updatedAt,url",
        ]
    )
    if isinstance(payload, list) and payload:
        first_run = payload[0]
        if isinstance(first_run, dict):
            return first_run
    return {}


def trigger_workflow(
    repo: str,
    workflow: str,
    dispatch_ref: str,
    inputs: dict[str, str],
) -> CommandResult:
    """触发 workflow_dispatch。

    Args:
        repo: owner/name。
        workflow: workflow 文件名。
        dispatch_ref: 触发使用的 git ref。
        inputs: workflow 输入参数。

    Returns:
        CommandResult: 命令执行结果。
    """
    arguments = ["workflow", "run", workflow, "--repo", repo, "--ref", dispatch_ref]
    for input_key, input_value in inputs.items():
        arguments.extend(["-f", f"{input_key}={input_value}"])
    return run_command(["gh", *arguments], timeout_seconds=60)
