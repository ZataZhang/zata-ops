---
name: log-query
description: 用登记在 zata-ops 服务清单里的只读 Grafana Token，经 HTTPS 查询集中式 Loki 日志（不走 SSH）：统计各应用错误量、按关键字检索、查看某服务最近日志或某个时间点前后的上下文、排查线上报错。不负责把服务器接入日志平台（container-observability-onboard），也不替代单机 SSH 现场排查（zata-ops-operations 的 logs 命令）。
---

# 集中日志查询（Loki）

经 Grafana 数据源代理只读查询 Loki。Token 由 zata-ops 凭据工具注入子进程，Agent 只引用变量名，不读取、不打印、不转述其值。

## 1. 找到查询入口

zata-ops 仓库根目录 = 本文件所在目录向上两级（经符号链接安装时先解析真实路径），以下命令都在该目录执行，例如 `cd "$(dirname "$(realpath <本文件>)")/../.."`。

```bash
just service-list
```

选 `type` 为 `http`、`notes` 说明用于查询 Loki 日志的记录（通常名为 `grafana-*`），记下 `url` 与 `password_env`，并确认：

- `password_configured` 为 `true`；否则请用户在自己终端运行 `just secret-set <password_env>`，Agent 不代为输入。
- `expiry_status` 不是 `expired`；`expiring` 时在交付中提醒用户按 `notes` 中的步骤轮换。

清单中有多条时按用户所指的平台或服务器（`server` 字段）选择；无法判断时询问用户，不要逐个尝试。

## 2. 调用方式

```bash
uv run python scripts/diagnostics/private_secrets.py run --env <password_env> --show-output -- \
  python3 skills/log-query/scripts/loki_query.py --base-url <url> --token-env <password_env> <子命令> ...
```

外层是凭据注入工具 `private_secrets.py`（注意不是服务清单脚本 `private_services.py`）；内层查询脚本只用标准库，故用系统 `python3` 而非 `uv run`。

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `labels [标签名]` | 列出标签名，或某标签的取值 | `--since 24h` |
| `metric '<LogQL>'` | 即时计数/聚合，只返回数字 | LogQL 内自带区间，如 `[1h]` |
| `logs '<LogQL>'` | 取日志原文，逐行截断，显示时最新在前 | `--since 1h`（默认）或 `--start/--end`（RFC3339，二者同给）；`--limit 50`（上限 500）；`--direction backward/forward` 决定超出 limit 时保留最新还是最早；`--max-chars 400` |

退出码：0 成功；1 查询失败（stderr 给出 HTTP 状态与 Loki 报错）；2 参数错误或 Token 未注入。输出末行 `values redacted` 来自注入工具，表示已屏蔽凭据原文。

## 3. 查询顺序：先确认标签，再计数，最后取少量原文

下文用 `<错误过滤>` 指代统一的错误关键字过滤：`|~ "(?i)(error|exception|traceback)"`，用 `<排除监控>` 指代 `compose_service!~"loki|grafana|vector"`（原因见第 4 节）。

1. **先确认标签取值，不要照搬清单或用户说法**：`labels node`、`labels compose_project`。服务清单的 `server`（如 `DWCS-AI2`）与 Loki 的 `node`（如 `dwcs-ai2`）大小写不一定相同；Dokploy 项目名带随机后缀（如 `dw-fsense-new-hvuk4k`），按前缀对应用户口中的应用名。
2. 用 `metric` 统计范围和量级：
   `sum by (compose_project, compose_service) (count_over_time({node="<节点>", <排除监控>} <错误过滤> [1h]))`
3. 锁定目标后用 `logs` 取原文，从 `--limit 20` 起步；需要更多时收窄时间或增加过滤，而不是放大 limit。
4. 需要异常前后的完整上下文（堆栈中间帧、同一请求的其他日志不含关键字）时，取异常时间点前后各十几秒，**去掉关键字过滤**：
   `logs '{compose_project="<项目>", compose_service="<服务>"}' --start <T-10s> --end <T+5s> --direction forward --limit 50`
5. 时间范围始终显式给出；用户说"刚才""最近"时默认 1h，再按需扩大。Loki 只保留约 7 天。

## 4. 标签与已知陷阱

| 标签 | 含义 |
|---|---|
| `node` | 服务器节点名（采集器的 `NODE_NAME`） |
| `compose_project` | Compose / Dokploy 项目名，即"哪个应用" |
| `compose_service` / `service_name` | 项目内的服务名（两者相同） |
| `level` | **目前不可靠**：非 JSON 日志一律为 `info`，不要用 `level="error"` 判断是否有错误，改用 `<错误过滤>` |

- **排除监控栈自身**：Loki 会把收到的查询语句写进自己的日志，关键字检索会命中自己，查得越多误报越多。凡是关键字过滤都带上 `<排除监控>`。
- **行数 ≠ 事件数**：一次 Python traceback 会拆成多行，按行数排名会偏向打印长堆栈的应用。报告时说明是"匹配行数"；需要估计事件数时改为只计 `|~ "Traceback \\(most recent call last\\)"` 或应用自己的 `ERROR` 前缀行，并用 `logs` 看时间戳是否聚集在同一时刻。
- 结果为空不等于"没有日志"：先核对标签取值和时间范围，再怀疑采集链路；采集链路排查属于 `container-observability-onboard`。
- 日志行通常是应用原始输出；`logs` 会从 Vector 写入的 JSON 中取出 `message` 显示。

## 5. 常用模板

```logql
# 某应用最近的错误原文
{compose_project="<项目>", compose_service!~"loki|grafana|vector"} |~ "(?i)(error|exception|traceback)"

# 各服务错误匹配行数排行
sum by (compose_project, compose_service) (count_over_time({node="<节点>", compose_service!~"loki|grafana|vector"} |~ "(?i)(error|exception|traceback)" [1h]))

# 各服务 Python 异常次数（近似事件数）
sum by (compose_project, compose_service) (count_over_time({node="<节点>", compose_service!~"loki|grafana|vector"} |~ "Traceback \\(most recent call last\\)" [1h]))

# 按请求 ID、用户 ID 等关键字定位（大小写敏感）
{compose_project="<项目>"} |= "<关键字>"

# 某服务的日志量（判断是否在持续输出、是否已停止）
sum(count_over_time({compose_service="<服务>"} [5m]))
```

## 6. 安全与交付

- 只读：Token 为 Viewer 权限，不尝试写入、删除或调用 Grafana 管理接口。
- 日志可能含用户数据、邮箱、内部地址：交付时给结论、计数和最少量的必要原文，不大段转贴；不把日志内容写入仓库、PR 或证据文件，除非用户明确要求并已脱敏。
- 认证失败按状态码处理，不反复重试：`401` 多为 Token 错误或已吊销，请用户重新 `just secret-set`；`403` 为权限不足，向用户说明，不要求升级为 Editor；`400` 为 LogQL 语法错误，按 stderr 修正。
- 交付时写明查询的平台、时间范围、使用的 LogQL，以及计数口径（匹配行数还是事件数），便于用户在 Grafana Explore 中复查。
