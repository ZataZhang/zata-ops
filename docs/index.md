# zata-ops

zata-ops 是主要面向 AI Agent 的轻量运维工具箱。通过本机私有配置管理服务器与应用信息，
提供可预览、可执行、可验证的命令行操作，帮助 Agent 完成日常查询、服务器与应用维护，
以及数据库备份恢复；人可以复现操作并检查结果。

建设优先级是打通「私有配置 → 定位目标 → 预览操作 → 执行 → 验证结果」，再扩展命令数量。
执行预览、结构化输出和操作后检查按命令逐步完善，具体支持范围以各命令文档为准。
网页控制台、常驻管理服务和多用户权限暂不作为建设重点。

工具代码、虚构配置示例和验证规则纳入版本管理，真实运维配置保留在本机。
服务器与应用清单目前支持记录和本地验证，尚未接入 CLI 自动化操作。
首次使用请阅读[私有运维配置](guides/private-inventory.md)。

`zata-ops` provides:

- `db backup` / `db restore` / `db list` — backups to any S3-compatible store.
- `db check` — S3 connectivity diagnostic.
- `db migrate` — PostgreSQL data migration (not Alembic).
- `env provision` / `env fix` — VPS + Traefik bootstrap and ACME repair.
- `logs tail` / `logs search` — Docker and systemd log inspection.
- `tunnel open` / `tunnel list` / `tunnel status` / `tunnel close` — manage background system `ssh` port-forwarding processes.
- `dashboard` — terminal status snapshot.

Install:

```bash
cd /path/to/zata-ops
uv tool install --force .
zata-ops --version
```

Read the [Backup & Restore guide](guides/backup-and-restore.md), the
[Scheduling examples](guides/scheduling.md), or the
[VPS provisioning guide](guides/vps-provisioning.md) to get started.

在 VPS 上运行 GitHub Actions，参见 [Self-hosted Runner 安装与维护](guides/github-actions-runner.md)。
