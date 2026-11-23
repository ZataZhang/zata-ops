"""根据忽略跟踪的实例配置生成 OpenBao Traefik 路由。"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

TEMPLATE_PATH = Path(__file__).with_name("openbao-dynamic.yml.in")
REQUIRED_CONFIG_KEYS = ("OPENBAO_INGRESS_DOMAIN", "OPENBAO_ALLOWED_SOURCE_CIDRS")
PLACEHOLDER_PATTERN = re.compile(r"@[A-Z_]+@")


def parse_allowed_networks(value: str) -> list[str]:
    """校验并去重来源 CIDR 列表。

    Args:
        value: 逗号分隔的来源网段。

    Returns:
        规范化且去重后的 CIDR 列表。

    Raises:
        ValueError: 列表为空或包含无效网段。
    """
    source_entries = [entry.strip() for entry in value.split(",")]
    if not source_entries or any(not entry for entry in source_entries):
        raise ValueError("invalid_source_cidr_list")

    source_networks: list[str] = []
    for source_entry in source_entries:
        try:
            source_network = str(ipaddress.ip_network(source_entry, strict=False))
        except ValueError as error:
            raise ValueError("invalid_source_cidr_list") from error
        if source_network not in source_networks:
            source_networks.append(source_network)
    return source_networks


def validate_domain(domain: str) -> str:
    """校验 DNS 域名，避免写入 Traefik 规则时注入额外语法。

    Args:
        domain: 实例的 API 域名。

    Returns:
        移除尾部句点后的域名。

    Raises:
        ValueError: 域名不是有效的多段 DNS 名称。
    """
    normalized_domain = domain.rstrip(".")
    domain_labels = normalized_domain.split(".")
    if len(domain_labels) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in domain_labels
    ):
        raise ValueError("invalid_ingress_domain")
    return normalized_domain


def load_ingress_settings(config_path: Path) -> tuple[str, list[str]]:
    """从 dotenv 文件读取域名和获准来源网段。

    Args:
        config_path: 被 Git 忽略的实例 ingress.env 路径。

    Returns:
        实例域名及校验、去重后的来源网段。

    Raises:
        ValueError: 文件无法读取、缺少配置或配置值无效。
    """
    try:
        config_values = dotenv_values(
            config_path,
            interpolate=False,
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("ingress_config_unreadable") from error

    missing_keys = [
        config_key
        for config_key in REQUIRED_CONFIG_KEYS
        if config_key not in config_values
    ]
    if missing_keys:
        raise ValueError("ingress_config_missing_required_value")

    ingress_domain_value = config_values["OPENBAO_INGRESS_DOMAIN"]
    source_cidrs_value = config_values["OPENBAO_ALLOWED_SOURCE_CIDRS"]
    if not ingress_domain_value:
        raise ValueError("ingress_config_missing_required_value")
    if source_cidrs_value is None:
        raise ValueError("invalid_source_cidr_list")
    ingress_domain = validate_domain(ingress_domain_value)
    allowed_networks = parse_allowed_networks(source_cidrs_value)
    return ingress_domain, allowed_networks


def render_route(
    template_text: str,
    ingress_domain: str,
    gateway_address: str,
    allowed_networks: list[str],
) -> str:
    """将已校验的实例值填入共享 Traefik 模板。

    Args:
        template_text: Traefik 动态路由模板内容。
        ingress_domain: 实例 API 域名。
        gateway_address: OpenBao ingress bridge 网关地址。
        allowed_networks: 获准访问 API 的来源 CIDR 列表。

    Returns:
        可直接交给 Traefik file provider 加载的 YAML 文本。

    Raises:
        ValueError: 网关地址无效或模板占位符不符合约定。
    """
    try:
        gateway_ip = ipaddress.ip_address(gateway_address)
    except ValueError as error:
        raise ValueError("invalid_ingress_gateway") from error
    gateway_host = f"[{gateway_ip}]" if gateway_ip.version == 6 else str(gateway_ip)
    source_entries = "\n".join(
        f'          - "{network}"' for network in allowed_networks
    )

    substitutions = {
        "@DOMAIN@": validate_domain(ingress_domain),
        "@INGRESS_GATEWAY@": gateway_host,
        "@SOURCE_RANGE_ENTRIES@": source_entries,
    }
    rendered_text = template_text
    for placeholder, replacement in substitutions.items():
        if rendered_text.count(placeholder) != 1:
            raise ValueError("invalid_traefik_template")
        rendered_text = rendered_text.replace(placeholder, replacement)
    if PLACEHOLDER_PATTERN.search(rendered_text):
        raise ValueError("unresolved_traefik_template_placeholder")
    return rendered_text


def render_configuration(
    config_path: Path,
    gateway_address: str,
    output_path: Path,
) -> int:
    """根据本机私有实例配置生成路由文件，并限制文件权限。

    Args:
        config_path: 被 Git 忽略的实例 ingress.env 路径。
        gateway_address: OpenBao ingress bridge 网关地址。
        output_path: 生成的 Traefik 动态配置文件路径。

    Returns:
        路由中获准访问的来源网段数量。

    Raises:
        OSError: 无法读取模板或写入输出文件。
        ValueError: 实例配置、网关地址或模板无效。
    """
    ingress_domain, allowed_networks = load_ingress_settings(config_path)
    try:
        template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("traefik_template_unreadable") from error
    rendered_text = render_route(
        template_text,
        ingress_domain,
        gateway_address,
        allowed_networks,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.touch(mode=0o600, exist_ok=True)
    output_path.chmod(0o600)
    output_path.write_text(rendered_text, encoding="utf-8")
    return len(allowed_networks)


def main() -> int:
    """解析命令行参数并生成实例路由文件。

    Returns:
        成功时返回 0，配置错误时返回 2。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, required=True, help="ignored instance ingress.env"
    )
    parser.add_argument(
        "--gateway", required=True, help="Docker ingress bridge gateway IP"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="rendered route file path"
    )
    arguments = parser.parse_args()

    try:
        source_count = render_configuration(
            arguments.config,
            arguments.gateway,
            arguments.output,
        )
    except (OSError, ValueError) as error:
        print(f"OpenBao Traefik render failed: {error}", file=sys.stderr)
        return 2
    print(f"Rendered OpenBao Traefik route for {source_count} allowed sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
