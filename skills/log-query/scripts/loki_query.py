"""经 Grafana 数据源代理只读查询 Loki，输出精简结果供 Agent 阅读。

Token 只从环境变量读取（由 private_secrets.py run --env 注入），不接受命令行参数，
避免出现在进程列表、Shell 历史或对话中。仅使用标准库，可在任意 Python 3.10+ 运行。

用法示例：
    loki_query.py --base-url https://grafana.example.com --token-env X_TOKEN labels
    loki_query.py ... labels compose_project
    loki_query.py ... metric 'sum by (compose_project) (count_over_time({node="n1"} |~ "(?i)error" [1h]))'
    loki_query.py ... logs '{compose_project="app"} |~ "(?i)error"' --since 1h --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MAX_LIMIT = 500
REQUEST_TIMEOUT_SECONDS = 30


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        argparse.ArgumentParser: 已注册 labels、metric、logs 子命令的解析器。
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="Grafana 根地址")
    parser.add_argument(
        "--token-env", required=True, help="保存 Grafana Token 的环境变量名"
    )
    parser.add_argument("--datasource-uid", default="loki", help="Loki 数据源 UID")
    commands = parser.add_subparsers(dest="command", required=True)

    labels_parser = commands.add_parser("labels", help="列出标签名，或某标签的取值")
    labels_parser.add_argument("label_name", nargs="?")
    labels_parser.add_argument("--since", default="24h")

    metric_parser = commands.add_parser("metric", help="即时指标查询（计数、聚合）")
    metric_parser.add_argument("logql")

    logs_parser = commands.add_parser("logs", help="取日志原文，默认最新在前")
    logs_parser.add_argument("logql")
    logs_parser.add_argument("--since", default="1h")
    logs_parser.add_argument(
        "--start", help="RFC3339 起点，如 2026-11-06T01:43:00Z；给出时忽略 --since"
    )
    logs_parser.add_argument("--end", help="RFC3339 终点，需与 --start 同时给出")
    logs_parser.add_argument("--limit", type=int, default=50)
    logs_parser.add_argument("--max-chars", type=int, default=400)
    logs_parser.add_argument(
        "--direction", choices=["backward", "forward"], default="backward"
    )
    return parser


def loki_get(
    base_url: str,
    datasource_uid: str,
    token_text: str,
    api_path: str,
    query_params: dict[str, str],
) -> dict:
    """调用 Grafana 数据源代理下的 Loki HTTP API。

    Args:
        base_url (str): Grafana 根地址。
        datasource_uid (str): Loki 数据源 UID。
        token_text (str): Grafana Service Account Token。
        api_path (str): Loki API 路径，如 ``query_range``。
        query_params (dict[str, str]): 查询参数。

    Returns:
        dict: 解析后的 JSON 响应。

    Raises:
        RuntimeError: HTTP 失败或响应不是成功状态时抛出，消息不含 Token。
    """
    request_url = (
        f"{base_url.rstrip('/')}/api/datasources/proxy/uid/{datasource_uid}"
        f"/loki/api/v1/{api_path}?{urllib.parse.urlencode(query_params)}"
    )
    http_request = urllib.request.Request(
        request_url, headers={"Authorization": f"Bearer {token_text}"}
    )
    try:
        with urllib.request.urlopen(
            http_request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as http_response:
            response_body = json.loads(http_response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        # Loki 的错误正文通常是一句 LogQL 解析错误，截断后返回便于修正查询。
        error_text = http_error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {http_error.code}: {error_text}") from None
    except urllib.error.URLError as url_error:
        raise RuntimeError(f"连接失败: {url_error.reason}") from None
    if response_body.get("status") != "success":
        raise RuntimeError(f"查询未成功: {str(response_body)[:300]}")
    return response_body


def format_timestamp(nanoseconds_text: str) -> str:
    """把 Loki 纳秒时间戳转换为 UTC ISO 时间。

    Args:
        nanoseconds_text (str): 纳秒级 Unix 时间戳字符串。

    Returns:
        str: 秒级精度的 UTC 时间文本。
    """
    return datetime.fromtimestamp(
        int(nanoseconds_text) / 1e9, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def print_labels(response_body: dict) -> None:
    """输出标签名或标签取值列表。

    Args:
        response_body (dict): Loki labels/label values 响应。
    """
    label_values_list: list[str] = response_body.get("data") or []
    print(f"{len(label_values_list)} 项")
    for label_value in label_values_list:
        print(label_value)


def print_metric(response_body: dict) -> None:
    """输出即时指标结果，丢弃 stats 等统计字段。

    Args:
        response_body (dict): Loki query 响应。
    """
    result_series_list: list[dict] = response_body["data"].get("result") or []
    if not result_series_list:
        print("无匹配（结果为空，不代表没有日志；请核对标签和时间范围）")
        return
    for result_series in sorted(
        result_series_list,
        key=lambda series: float(series.get("value", [0, "0"])[1]),
        reverse=True,
    ):
        metric_labels_text = ", ".join(
            f"{label_name}={label_value}"
            for label_name, label_value in sorted(result_series["metric"].items())
        )
        print(f"{result_series['value'][1]}\t{metric_labels_text or '(总计)'}")


def print_logs(response_body: dict, max_chars: int) -> None:
    """输出日志行，每行带时间与来源并截断过长内容。

    Args:
        response_body (dict): Loki query_range 日志流响应。
        max_chars (int): 单行最多保留的字符数。
    """
    log_entries_list: list[tuple[str, str, str]] = []
    for log_stream in response_body["data"].get("result") or []:
        stream_labels: dict = log_stream["stream"]
        source_text = "/".join(
            stream_labels.get(label_name, "?")
            for label_name in ("node", "compose_project", "compose_service")
        )
        for timestamp_text, raw_line_text in log_stream["values"]:
            log_entries_list.append((timestamp_text, source_text, raw_line_text))
    # 多个流合并后按时间重新排序，最新在前。
    log_entries_list.sort(key=lambda log_entry: int(log_entry[0]), reverse=True)
    print(f"{len(log_entries_list)} 行（最新在前）")
    for timestamp_text, source_text, raw_line_text in log_entries_list:
        display_line_text = extract_message(raw_line_text).replace("\n", "\\n")
        if len(display_line_text) > max_chars:
            display_line_text = display_line_text[:max_chars] + "…(截断)"
        print(f"{format_timestamp(timestamp_text)} [{source_text}] {display_line_text}")


def extract_message(raw_line_text: str) -> str:
    """Vector 以 JSON 写入 Loki 时取出原始 message，否则原样返回。

    Args:
        raw_line_text (str): Loki 中存储的日志行。

    Returns:
        str: 便于阅读的日志正文。
    """
    try:
        parsed_line_dict = json.loads(raw_line_text)
    except ValueError:
        return raw_line_text
    if isinstance(parsed_line_dict, dict) and isinstance(
        parsed_line_dict.get("message"), str
    ):
        return parsed_line_dict["message"]
    return raw_line_text


def main() -> int:
    """执行查询并输出精简结果。

    Returns:
        int: 0 成功；1 查询失败；2 参数或 Token 缺失。
    """
    parsed_arguments = build_parser().parse_args()
    token_text = os.environ.get(parsed_arguments.token_env, "")
    if not token_text:
        print(
            f"环境变量 {parsed_arguments.token_env} 为空；请经 private_secrets.py run --env 注入。",
            file=sys.stderr,
        )
        return 2
    try:
        if parsed_arguments.command == "labels":
            api_path = (
                f"label/{urllib.parse.quote(parsed_arguments.label_name)}/values"
                if parsed_arguments.label_name
                else "labels"
            )
            print_labels(
                loki_get(
                    parsed_arguments.base_url,
                    parsed_arguments.datasource_uid,
                    token_text,
                    api_path,
                    {"since": parsed_arguments.since},
                )
            )
        elif parsed_arguments.command == "metric":
            print_metric(
                loki_get(
                    parsed_arguments.base_url,
                    parsed_arguments.datasource_uid,
                    token_text,
                    "query",
                    {"query": parsed_arguments.logql},
                )
            )
        else:
            if not 1 <= parsed_arguments.limit <= MAX_LIMIT:
                print(f"--limit 须在 1–{MAX_LIMIT} 之间。", file=sys.stderr)
                return 2
            if bool(parsed_arguments.start) != bool(parsed_arguments.end):
                print("--start 与 --end 需同时给出。", file=sys.stderr)
                return 2
            # 绝对时间窗口用于查看某个事件前后的上下文；否则按相对 --since。
            time_range_params: dict[str, str] = (
                {"start": parsed_arguments.start, "end": parsed_arguments.end}
                if parsed_arguments.start
                else {"since": parsed_arguments.since}
            )
            print_logs(
                loki_get(
                    parsed_arguments.base_url,
                    parsed_arguments.datasource_uid,
                    token_text,
                    "query_range",
                    {
                        "query": parsed_arguments.logql,
                        **time_range_params,
                        "limit": str(parsed_arguments.limit),
                        "direction": parsed_arguments.direction,
                    },
                ),
                parsed_arguments.max_chars,
            )
    except RuntimeError as query_error:
        print(f"查询失败：{query_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
