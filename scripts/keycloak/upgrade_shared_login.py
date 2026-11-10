"""为已有 Keycloak Realm 配置共享登录，默认仅预览。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

KEYCLOAK_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keycloak"
REALM_FIELDS = ("browserFlow", "loginTheme", "registrationAllowed")
PROVIDER_ID = "shared-otp-authenticator"
FLOW_ALIAS = "shared-browser"


class RealmUpgrade:
    """通过容器内已登录的 Admin CLI 操作一个 Realm。

    Args:
        options: 容器、Realm、快照和执行模式参数。
    """

    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options

    def command(self, arguments: list[str], payload: Any = None) -> Any:
        """执行 Admin CLI，抑制可能携带敏感内容的错误正文。

        Args:
            arguments: Admin CLI 参数。
            payload: 通过标准输入传递的 JSON。

        Returns:
            JSON 响应或空值。

        Raises:
            RuntimeError: CLI 执行失败。
        """
        invocation = [
            "docker",
            "exec",
            "-i",
            self.options.container,
            "/opt/keycloak/bin/kcadm.sh",
            *arguments,
            "--config",
            self.options.config,
        ]
        completed = subprocess.run(
            invocation,
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(
                "Admin CLI failed for "
                + " ".join(arguments[:2])
                + "; check authentication, permissions and server logs"
            )
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def read(self, endpoint: str) -> Any:
        """读取目标 Realm 的资源。

        Args:
            endpoint: Admin API 相对路径。

        Returns:
            JSON 资源。
        """
        return self.command(["get", endpoint, "-r", self.options.realm])

    def write(self, method: str, endpoint: str, payload: Any) -> None:
        """以标准输入提交 JSON。

        Args:
            method: create 或 update。
            endpoint: Admin API 相对路径。
            payload: 请求 JSON。
        """
        self.command([method, endpoint, "-r", self.options.realm, "-f", "-"], payload)

    def validate_flow(self, executions: list[dict[str, Any]]) -> None:
        """拒绝复用含额外步骤或不同策略的同名流程。

        Args:
            executions: 服务器流程执行项。

        Raises:
            RuntimeError: 同名流程与版本定义不一致。
        """
        expected = [("auth-cookie", "ALTERNATIVE"), (PROVIDER_ID, "ALTERNATIVE")]
        actual = [
            (execution.get("providerId"), execution.get("requirement"))
            for execution in executions
        ]
        if actual != expected:
            raise RuntimeError(
                "Existing shared-browser differs from the expected flow; no changes applied"
            )

    def restore(self, snapshot: dict[str, Any]) -> None:
        """恢复此前的绑定、主题、注册策略和资料设置。

        Args:
            snapshot: 本命令保存的配置快照。
        """
        if snapshot.get("realm") != self.options.realm:
            raise RuntimeError("Snapshot belongs to another realm")
        self.write("update", "realms/" + self.options.realm, snapshot["settings"])
        self.write("update", "users/profile", snapshot["profile"])
        restored = self.read("realms/" + self.options.realm)
        if any(
            restored.get(key, "") != value
            for key, value in snapshot["settings"].items()
        ):
            raise RuntimeError("Rollback verification failed")
        if self.read("users/profile") != snapshot["profile"]:
            raise RuntimeError("Profile rollback verification failed")

    def run(self) -> None:
        """执行只读预览、显式升级或恢复，并验证服务器结果。

        Raises:
            RuntimeError: 前置条件、快照或结果检查失败。
        """
        if self.options.rollback:
            snapshot = json.loads(self.options.snapshot.read_text(encoding="utf-8"))
            if self.options.apply:
                self.restore(snapshot)
                print(
                    "Rollback verified; the shared flow remains available but is no longer bound."
                )
            else:
                print(
                    "PREVIEW: restore settings and user profile from the specified snapshot."
                )
            return

        realm = self.read("realms/" + self.options.realm)
        providers = self.read("authentication/authenticator-providers")
        if not any(provider.get("id") == PROVIDER_ID for provider in providers):
            raise RuntimeError(
                "Shared OTP Provider is missing. Deploy the custom Keycloak image first."
            )
        theme_check = subprocess.run(
            [
                "docker",
                "exec",
                self.options.container,
                "test",
                "-f",
                "/opt/keycloak/themes/shared/login/theme.properties",
            ],
            check=False,
            capture_output=True,
        )
        if theme_check.returncode:
            raise RuntimeError("The shared login theme is missing from the container")
        profile = self.read("users/profile")
        flows = self.read("authentication/flows")
        flow_exists = any(flow.get("alias") == FLOW_ALIAS for flow in flows)
        if flow_exists:
            self.validate_flow(
                self.read("authentication/flows/" + FLOW_ALIAS + "/executions")
            )
        # 保留现有自定义字段、验证器和权限，仅解除邮箱和姓名的必填约束。
        target_profile = json.loads(json.dumps(profile))
        for attribute in target_profile.get("attributes", []):
            if attribute.get("name") in {"email", "firstName", "lastName"}:
                attribute.pop("required", None)
        target_settings = dict(
            browserFlow=FLOW_ALIAS, loginTheme="shared", registrationAllowed=True
        )
        print("Target realm: " + self.options.realm)
        print(
            "Provider and theme: available; flow: "
            + ("validated" if flow_exists else "will be created")
        )
        print(
            "Plan: enable shared browser login and self-registration; make email/name fields optional."
        )
        print(
            "SMTP, SMS credentials, clients, users and passwords are outside this configuration patch."
        )
        if not self.options.apply:
            print("PREVIEW ONLY. Add --apply with a new --snapshot path to execute.")
            return
        snapshot = {
            "realm": self.options.realm,
            "settings": {key: realm.get(key, "") for key in REALM_FIELDS},
            "profile": profile,
        }
        descriptor = os.open(
            self.options.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as snapshot_file:
            json.dump(snapshot, snapshot_file, indent=2)
        try:
            if not flow_exists:
                flow_definition = json.loads(
                    (KEYCLOAK_DIR / "realm/realm.json").read_text(encoding="utf-8")
                )["authenticationFlows"][0]
                self.write(
                    "create",
                    "authentication/flows",
                    {
                        key: value
                        for key, value in flow_definition.items()
                        if key != "authenticationExecutions"
                    },
                )
                endpoint = "authentication/flows/" + FLOW_ALIAS + "/executions"
                for definition in flow_definition["authenticationExecutions"]:
                    self.write(
                        "create",
                        endpoint + "/execution",
                        {"provider": definition["authenticator"]},
                    )
                for execution in self.read(endpoint):
                    self.write(
                        "update", endpoint, {**execution, "requirement": "ALTERNATIVE"}
                    )
                self.validate_flow(self.read(endpoint))
            self.write("update", "users/profile", target_profile)
            self.write("update", "realms/" + self.options.realm, target_settings)
            updated = self.read("realms/" + self.options.realm)
            if any(updated.get(key) != value for key, value in target_settings.items()):
                raise RuntimeError("Realm settings verification failed")
            if self.read("users/profile") != target_profile:
                raise RuntimeError("Profile verification failed")
        except Exception:
            self.restore(snapshot)
            raise
        print("Upgrade verified. Snapshot: " + str(self.options.snapshot))


def main() -> int:
    """解析 CLI 并运行升级。

    Returns:
        成功为 0，失败为 1；参数错误由 argparse 返回 2。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--config", default="/tmp/shared-login-kcadm.config")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    options = parser.parse_args()
    try:
        RealmUpgrade(options).run()
    except (RuntimeError, OSError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
