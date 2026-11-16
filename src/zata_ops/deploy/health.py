"""Small HTTP health probe used by ``zata-ops deploy status``."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any


def probe_health(health_url: str, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """GET 一个健康检查地址并返回结构化结果。

    Args:
        health_url: 完整的 http(s) 地址。
        timeout_seconds: 请求超时时间。

    Returns:
        dict: 含 ok / status / latency_ms，失败时附带 error。
    """
    started_at = time.monotonic()
    request = urllib.request.Request(
        health_url,
        headers={"User-Agent": "zata-ops-deploy/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        return _failure_result(exc.code, started_at, f"HTTPError: {exc.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _failure_result(None, started_at, f"{type(exc).__name__}: {exc}")

    return {
        "ok": 200 <= status_code < 400,
        "status": status_code,
        "latency_ms": round((time.monotonic() - started_at) * 1000),
        "url": health_url,
    }


def _failure_result(
    status_code: int | None,
    started_at: float,
    error_text: str,
) -> dict[str, Any]:
    """构造健康检查失败结果。"""
    return {
        "ok": False,
        "status": status_code,
        "latency_ms": round((time.monotonic() - started_at) * 1000),
        "error": error_text[:300],
    }
