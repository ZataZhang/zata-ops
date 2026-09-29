---
name: zata-ops-operations
description: 使用 zata-ops 处理 SSH 隧道、数据库备份/恢复/跨机迁移、日志排查和 VPS 初始化；适用于询问或执行本仓库已有运维 CLI 工作流，不替代专门的 GitHub CD 或监控接入 skill。
---

# zata-ops 运维操作

帮助用户从现有 `zata-ops` CLI 和服务器工具完成操作，并报告实际验证到的结果。先按仓库 `AGENTS.md` 读取必要规范，再确认当前安装版本的 `zata-ops --help` 和相关子命令 `--help`。README 中可能保留旧用法；具体参数以当前 CLI 实现和帮助为准。

## 按任务查阅

| 任务 | 仓库权威入口 | 操作要点 |
|---|---|---|
| SSH 本地/远端转发 | `docs/guides/tunnel.md`、`src/zata_ops/tunnel/cli.py` | `tunnel open <name> -- ssh ...` 后台启动；`list/status/close` 管理。认证、转发和重连由系统 `ssh` 负责。 |
| PostgreSQL 跨机迁移 | [数据库迁移](references/database-migration.md)、`src/zata_ops/db/cli.py` | 区分复制快照与应用切换，核对目标库和持续写入。 |
| S3 备份、恢复和检查 | `docs/guides/backup-and-restore.md`、`src/zata_ops/db/cli.py` | `db check` 检查 S3，**不是**数据库连通性测试；恢复参数可能替换目标数据。 |
| VPS 初始化和 ACME 修复 | `docs/guides/vps-provisioning.md`、`src/zata_ops/env/cli.py` | 先看 `--dry-run` 的远端计划，再按用户授权执行。 |
| 容器或 systemd 日志 | `src/zata_ops/logs/cli.py` | `logs tail/search` 可先用 `--dry-run` 确认命令；输出可能含业务敏感信息。 |
| 终端状态面板 | `src/zata_ops/observability/cli.py` | 当前 `dashboard` 使用模拟数据，不能当作真实健康检查。 |

GitHub Actions runner 主机安装和工作流调度适配用 `github-self-hosted-runner` skill；应用发布流程用 `github-vps-deploy` skill；应用日志和指标接入用 `container-observability-onboard` skill。不要把这些专项流程复制到本 skill。

## SSH 隧道

先核对 SSH 主机身份、目标主机、远端服务监听地址和本地端口占用。数据库只在远端 loopback 监听时，可使用：

```bash
zata-ops tunnel open db-access -- \
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:6669:127.0.0.1:5432 user@host
zata-ops tunnel status db-access
# 数据库客户端另行连接 127.0.0.1:6669，并验证登录和只读查询。
zata-ops tunnel close db-access
```

`open` 返回只代表后台进程已启动；`status`/PID 存活也不能证明数据库登录成功。按用户请求测试实际服务入口，结束短时任务后关闭自己创建的隧道。需要长期重连时参照隧道指南，不能把普通 `tunnel open` 当成自动重连器。SSH 参数不得包含数据库密码。

## 操作边界和交付

- 查明任务是查看、备份、复制、恢复、切换还是删除；只读问题不触发外部写操作。已明确授权的操作继续完成，不为例行只读检查重复询问。
- 连接串中的密码、SSH 私钥、token 不写入仓库、命令参数、输出或日志。优先使用受限配置文件、环境变量或交互输入；展示连接串时遮盖密码。不要把真实服务器地址和凭据写进可复用示例。
- 对会覆盖数据库、修改 VPS 或切换应用的步骤，先确认具体目标和现状；保留可恢复路径。快照导入成功不等于应用已切换或源库写入已同步。
- 交付时区分“命令已启动”“隧道可用”“服务认证成功”“数据核对通过”，说明实际完成到哪一步，以及留下的资源或后续切换事项。
