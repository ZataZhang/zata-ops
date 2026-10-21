# 应用现场检查

`app inspect` 把私有清单中的一个部署实例与本次实际观察分开报告，供 Agent 判断下一步。
它不修改清单、不启动或重启服务、不执行部署，也不保存检查历史。

## 使用

先按[私有运维配置](private-inventory.md)准备清单，然后在仓库根目录运行：

```bash
# 只解析配置与展示检查计划，不连接服务器、不发送 HTTP 请求
zata-ops app inspect example-app --server example-server --dry-run

# 执行只读现场检查
zata-ops app inspect example-app --server example-server

# 在其他工作目录或安装为独立工具时显式指定清单
zata-ops app inspect example-app --inventory /path/to/private/servers.toml
```

默认清单路径是当前目录下的 `config/private/servers.toml`，不自动搜索上级目录，
也不从 `.env` 推断目标。应用名称必须精确匹配；多个服务器上有同名应用时，
省略 `--server` 会报 `target_ambiguous`，不会默认选择第一个。

CLI 只读加载 TOML，不运行 Git 私有性校验；存放配置时仍需按私有配置指南确保忽略与权限，
可先运行 `just inventory-check` 验证。公开示例只能包含虚构信息。

## 检查边界

| 检查 | 方法 | 结果含义 |
| --- | --- | --- |
| `ssh` | 系统 `ssh` 执行 `true` | 本次可连接并执行命令；不沿用清单中的历史 verified 状态 |
| `runtime`，Docker Compose | 按 `deployment_id` 匹配 `com.docker.compose.project` 标签，查询 `docker ps --all` | 报告实际匹配容器数量、状态计数和 unhealthy 数量 |
| `runtime`，systemd | `systemctl is-active -- <deployment_id>` | 当前 unit 是否 active；该字段必须填写准确的 unit 名称 |
| `http` | 从本机对应用 `url` 发起 HEAD | 200–399 为端点有响应，400 以上为失败；405/501 表示方法不支持，记为未知 |

SSH 优先使用 `public_ip`，为空时使用 `private_ip`，按照清单的用户、端口和可选私钥路径连接。
`identity_file` 当前支持文件路径（可含 `~`）；未填写时由本机 SSH 配置和 agent 认证，
不解析凭据库引用。使用批处理、严格主机密钥校验，禁止端口转发和 LocalCommand；
保留本机 SSH 配置以支持已有 ProxyJump。首次主机信任或凭据准备需提前完成，检查不会交互询问密码或接受新主机密钥。

每个 SSH/HTTP 探测的超时为 10 秒，三个探测顺序运行。SSH 失败时运行状态记为未知，
HTTP 仍单独检查。远端 Docker 权限不足、命令缺失或输出不可解析时记为未知，
不会把采集失败直接解释为应用停止。远端只执行固定的状态查询，应用标识经 shell quoting 传递。

Compose 检查仅证明**实际观察到的容器**在运行：停止的容器或 unhealthy 导致失败，
健康检查仍在 starting 时记为未知。它不读取 Compose 文件，不知道预期副本数、缺失服务或
已删除容器；完成后保留的退出态一次性任务也会使检查失败。没有 Docker healthcheck 的
running 容器只代表进程运行，不代表业务健康。systemd active 同样不代表业务健康。

HTTP 保留默认 TLS 证书验证与系统代理行为，不附带应用认证，不跟随重定向、不下载响应正文，
不输出 URL、响应头或响应正文。重定向响应仅证明原端点有响应，401/403 也只说明本次未认证检查失败。
如果需要经过认证的业务验证，应使用专门的验证流程。当前不检查备份新鲜度、可恢复性、
数据库连通性、端口列表或应用完整业务链路。

## 输出与退出码

标准输出为单份 JSON，包含 `schema_version: 1`、`operation: "app.inspect"`、
`target`、`inventory` 和 `checks`。其中：

- `inventory` 是人工记录，包括清单更新时间、盘点状态和应用历史验证时间，均不代表本次验证。
- 每项 `checks` 有 `name`、`status`、`reason`、`method`、`checked_at` 与 `evidence`。
- `checked_at` 使用本机 UTC 时间；不同探测不是原子快照。
- 失败不输出原始 SSH stderr、配置值或 Python 异常正文；报告仍包含应用与服务器别名及历史验证时间，应作为私有信息处理。
- 配置加载或目标定位失败时输出简短 JSON 错误；参数语法错误沿用 Typer 的终端错误格式。

| 退出码 | 含义 |
| --- | --- |
| `0` | 三项检查均通过，或成功生成 dry-run 计划（不表示应用健康） |
| `1` | 至少有一项观察到失败，整体 `failed` |
| `2` | 清单非法、目标不存在/不唯一，或参数用法错误 |
| `3` | 没有已观察失败，但存在未知项，整体 `incomplete` |

预览整体状态为 `planned`；配置不足的检查仍显示 `unknown`，其他检查显示 `planned`。
预览中的时间仅为计划生成时间，不能用作远端检查证据。
