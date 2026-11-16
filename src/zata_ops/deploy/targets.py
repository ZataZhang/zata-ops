"""Deploy target loading from the private inventory.

真实仓库名、环境和健康检查地址属于私有运维信息，只放在
``config/private/deploy_targets.toml``（已被 .gitignore 忽略）。
本模块只定义契约与校验，不内置任何项目常量。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TARGETS_FILE = Path("config/private/deploy_targets.toml")

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DeployTargetError(ValueError):
    """部署 target 配置缺失或非法。"""


@dataclass(frozen=True)
class DeployTarget:
    """一个可部署目标的静态配置。

    Attributes:
        name: 命令行使用的 target 名称。
        repo: GitHub 仓库，格式 owner/name。
        workflow: CD workflow 文件名，例如 cd.yml。
        environment: GitHub Environment 名称，例如 staging / production。
        dispatch_ref: workflow_dispatch 使用的 git ref，默认 main。
        health_url: 可选的外部健康检查 URL。
        production: 是否生产环境，仅用于提示与确认文案。
    """

    name: str
    repo: str
    workflow: str
    environment: str
    dispatch_ref: str = "main"
    health_url: str = ""
    production: bool = False


def validate_image_tag(image_tag: str) -> str:
    """校验回滚镜像 tag，避免把任意字符串拼进命令行。

    Args:
        image_tag: 目标镜像 tag，例如 ``<sha>-production``。

    Returns:
        str: 校验通过的 tag。

    Raises:
        DeployTargetError: tag 含非法字符或为空。
    """
    if not _TAG_PATTERN.fullmatch(image_tag or ""):
        raise DeployTargetError(f"非法的镜像 tag: {image_tag!r}")
    return image_tag


def load_deploy_targets(
    targets_path: Path = DEFAULT_TARGETS_FILE,
) -> dict[str, DeployTarget]:
    """读取并校验私有部署 target 文件。

    Args:
        targets_path: TOML 路径，默认 ``config/private/deploy_targets.toml``。

    Returns:
        dict: target 名称到配置对象的映射。

    Raises:
        DeployTargetError: 文件缺失、TOML 非法或字段不完整。
    """
    if not targets_path.is_file():
        raise DeployTargetError(f"部署 target 文件不存在: {targets_path}")

    try:
        with targets_path.open("rb") as targets_handle:
            raw_config = tomllib.load(targets_handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DeployTargetError(
            f"部署 target 文件无法解析: {targets_path}: {exc}"
        ) from exc

    raw_targets = raw_config.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise DeployTargetError(
            f"部署 target 文件缺少非空 targets 列表: {targets_path}"
        )

    targets: dict[str, DeployTarget] = {}
    for raw_target in raw_targets:
        target = _parse_target(raw_target)
        if target.name in targets:
            raise DeployTargetError(f"部署 target 名称重复: {target.name}")
        targets[target.name] = target
    return targets


def resolve_deploy_target(
    target_name: str,
    targets_path: Path = DEFAULT_TARGETS_FILE,
) -> DeployTarget:
    """按名称取出一个部署 target。

    Args:
        target_name: 目标名称。
        targets_path: TOML 路径。

    Returns:
        DeployTarget: 匹配的配置。

    Raises:
        DeployTargetError: 文件不可用或名称不存在。
    """
    targets = load_deploy_targets(targets_path)
    target = targets.get(target_name)
    if target is None:
        available_names = "、".join(sorted(targets))
        raise DeployTargetError(
            f"未找到部署 target: {target_name}（可用: {available_names}）"
        )
    return target


def _parse_target(raw_target: object) -> DeployTarget:
    """把一条 TOML 记录转换成 DeployTarget。"""
    if not isinstance(raw_target, dict):
        raise DeployTargetError("targets 列表中的每一项必须是 TOML 表")

    name = _require_text(raw_target, "name")
    repo = _require_text(raw_target, "repo")
    workflow = _require_text(raw_target, "workflow")
    environment = _require_text(raw_target, "environment")

    if not _REPO_PATTERN.fullmatch(repo):
        raise DeployTargetError(f"target {name} 的 repo 必须是 owner/name: {repo!r}")

    dispatch_ref = str(raw_target.get("dispatch_ref", "main") or "main")
    health_url = str(raw_target.get("health_url", "") or "")
    production = bool(raw_target.get("production", environment == "production"))

    return DeployTarget(
        name=name,
        repo=repo,
        workflow=workflow,
        environment=environment,
        dispatch_ref=dispatch_ref,
        health_url=health_url,
        production=production,
    )


def _require_text(raw_target: dict, field_name: str) -> str:
    """读取必填字符串字段，缺失或为空时报错。"""
    field_value = raw_target.get(field_name)
    if not isinstance(field_value, str) or not field_value.strip():
        raise DeployTargetError(f"部署 target 缺少必填字段 {field_name}")
    return field_value.strip()
