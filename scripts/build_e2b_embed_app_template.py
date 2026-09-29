"""从应用模板定义构建并验证本地 E2B 模板。"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from pathlib import Path

from e2b import Sandbox, Template

_APP_TEMPLATE_ALIAS_PREFIX = "zata-assistant-runtime"


def _available_template_alias(base_alias: str, api_options: dict[str, str]) -> str:
    """避开前次失败后仍占用的别名，并复用应用配置中已就绪的别名。"""
    configured_template_id = os.getenv("E2B_TEMPLATE_ID", "").strip()
    if configured_template_id == base_alias or configured_template_id.startswith(
        f"{base_alias}-retry-"
    ):
        return configured_template_id
    if not Template.exists(base_alias, **api_options):
        return base_alias

    source_hash = base_alias.rsplit("-", 1)[-1]
    retry_number = 1
    while True:
        retry_alias = (
            f"{_APP_TEMPLATE_ALIAS_PREFIX}-arm64-retry-{retry_number}-{source_hash}"
        )
        if not Template.exists(retry_alias, **api_options):
            return retry_alias
        retry_number += 1


def main() -> int:
    """在本地 Embed 中构建应用模板并验证其运行时环境。"""
    if len(sys.argv) != 2:
        print(
            "Usage: build_e2b_embed_app_template.py <application-root>",
            file=sys.stderr,
        )
        return 2

    application_root = Path(sys.argv[1]).resolve()
    dockerfile_path = application_root / "deploy/sandbox/Dockerfile.e2b-template"
    if not dockerfile_path.is_file():
        raise RuntimeError(f"应用 E2B 模板 Dockerfile 不存在：{dockerfile_path}")

    e2b_api_key = os.getenv("E2B_API_KEY", "").strip()
    e2b_api_url = os.getenv("E2B_API_URL", "").strip()
    e2b_sandbox_url = os.getenv("E2B_SANDBOX_URL", "").strip()
    if not e2b_api_key or not e2b_api_url or not e2b_sandbox_url:
        raise RuntimeError("E2B_API_KEY、E2B_API_URL 和 E2B_SANDBOX_URL 都必须配置")

    api_options = {
        "api_key": e2b_api_key,
        "api_url": e2b_api_url,
        "sandbox_url": e2b_sandbox_url,
    }
    dockerfile_content = dockerfile_path.read_text(encoding="utf-8")
    dockerfile_hash = hashlib.sha256(
        f"arm64\n{dockerfile_content}".encode("utf-8")
    ).hexdigest()[:12]
    template_alias = f"{_APP_TEMPLATE_ALIAS_PREFIX}-arm64-dockerfile-{dockerfile_hash}"
    with contextlib.redirect_stdout(sys.stderr):
        template_definition = Template().from_dockerfile(str(dockerfile_path))
    template_source_description = str(dockerfile_path)

    template_alias = _available_template_alias(template_alias, api_options)
    if Template.exists(template_alias, **api_options):
        template_id = template_alias
    else:
        print(
            f"Building local E2B template {template_alias} from "
            f"{template_source_description}...",
            file=sys.stderr,
        )
        template_build = Template.build(
            template_definition,
            alias=template_alias,
            on_build_logs=lambda build_log_entry: print(
                str(build_log_entry), file=sys.stderr
            ),
            **api_options,
        )
        template_id = template_build.template_id

    print("Verifying the template in a temporary sandbox...", file=sys.stderr)
    test_sandbox = Sandbox.create(
        template=template_id,
        timeout=300,
        allow_internet_access=False,
        **api_options,
    )
    try:
        smoke_result = test_sandbox.commands.run(
            'set -eu; test "$(id -u)" != 0; '
            "test -w /workspace/inputs; test -w /workspace/outputs; "
            "test -w /large_tool_results; test -w /conversation_history; "
            "touch /large_tool_results/.template-smoke-test; "
            "rm /large_tool_results/.template-smoke-test; "
            'python -c "import openpyxl,pandas,numpy,scipy"; node --version'
        )
        if smoke_result.exit_code != 0:
            raise RuntimeError(
                "模板 smoke command 失败："
                f"exit_code={smoke_result.exit_code}; stderr={smoke_result.stderr}"
            )
    finally:
        test_sandbox.kill()

    print(template_alias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
