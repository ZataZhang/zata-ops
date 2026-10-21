# zata-ops

zata-ops 是主要面向 AI Agent 的轻量运维工具箱。通过本机私有配置管理服务器与应用信息，提供可预览、可执行、可验证的命令行操作，帮助 Agent 完成日常查询、服务器与应用维护，以及数据库备份恢复；人可以复现操作并检查结果。

## 设计理念

建设优先级是打通「私有配置 → 定位目标 → 预览操作 → 执行 → 验证结果」，再扩展命令数量。
执行预览、结构化输出和操作后检查按命令逐步完善，具体支持范围以各命令文档为准。

- **面向 Agent 使用**：以 CLI、明确的配置约定和操作文档为主要入口，逐步完善执行预览、结构化结果与操作后检查。
- **保持轻量**：优先把常用运维任务做成可重复执行的命令；网页控制台、常驻管理服务和多用户权限暂不作为建设重点。
- **工具与私有配置分开**：工具代码、虚构示例和验证规则纳入版本管理；真实服务器地址、SSH 凭据引用和应用部署清单保留在本机被 Git 忽略的配置中。

目前提供数据库备份与恢复、S3 连通性检查、VPS 环境初始化、日志查看、SSH 隧道管理与终端状态面板。
服务器与应用清单目前支持记录和本地验证，尚未接入 CLI 自动化操作。配置方式见[私有运维配置](docs/guides/private-inventory.md)。

---

## 安装

需要 Python >= 3.11 与 `uv`。

```bash
# 基础安装
uv tool install --force /path/to/zata-ops
zata-ops --version
```

需要 SSH 远程操作 VPS 或使用 `tunnel` 时，确保本机安装并配置了系统 `ssh` 命令。

仓库内的 [zata-ops 运维 Skill](skills/zata-ops-operations/SKILL.md) 可按任务引导使用隧道、数据库、日志及 VPS 命令。

升级到新版本:

```bash
uv tool install --force --reinstall /path/to/zata-ops
```

---

## 快速上手

所有命令会读取**当前工作目录**下的 `.env` 与 `.env.local`;命令行参数(`--xxx`)的优先级最高。带 `--dry-run` 的命令建议**先预览再执行**,避免误操作打到线上。

### 1. 准备 `.env`

复制本仓库或下游项目中的 `.env.example` 为 `.env`,至少填好与备份相关的字段:

```env
PROJECT=my-app
DATABASE_URL=postgresql://user:pass@host:5432/dbname
S3_ENDPOINT=https://s3.example.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=app-backups
S3_PREFIX=app-backups
S3_ADDRESSING_STYLE=path
RETENTION_DAYS=30
FULL_BACKUP_DAY=6
```

### 2. 验证 S3 连通

```bash
zata-ops db check
```

### 3. 备份数据库

```bash
# 先看会做什么
zata-ops db backup --dry-run

# 真正执行(周日全量,其余日子增量)
zata-ops db backup

# 强制本次做全量
zata-ops db backup --force-full
```

### 4. 查看与恢复

```bash
# 列出可用的备份时间点
zata-ops db list

# 恢复到指定时间点(恢复 DB,跳过确认)
zata-ops db restore --from 2026-06-07_180000 --restore-db --yes
```

---

## 命令参考

执行 `zata-ops <command> --help` 查看每个子命令的完整参数。

### 数据库 `db`

| 命令 | 用途 |
|---|---|
| `zata-ops db backup [--force-full] [--dry-run]` | 备份 DB + 日志 + 资源到 S3,按 `RETENTION_DAYS` 自动清理旧备份 |
| `zata-ops db list` | 列出 S3 上可用的备份时间点与类型 |
| `zata-ops db restore --from <时间戳> [--restore-db] [--restore-logs] [--restore-resources] [--yes]` | 从 S3 恢复,支持链式恢复 |
| `zata-ops db check` | 验证 S3 端点连通性与凭据 |
| `zata-ops db migrate --source <url> --target <url> [--dry-run] [--verify-row-counts] [--keep-dump]` | 在两个 PostgreSQL 之间迁移数据(非 Alembic；复制快照) |

迁移前检查、凭据和容器网络路径见[备份与恢复指南](docs/guides/backup-and-restore.md#postgresql-跨机迁移)。

### VPS 环境 `env`

```bash
# 初始化一台新 VPS:装 Docker、部署 Traefik
zata-ops env provision --host example.com --user deploy --dry-run
zata-ops env provision --host example.com --user deploy

# 同时部署监控栈(Vector + Loki + Prometheus + Grafana)
zata-ops env provision \
  --host example.com \
  --user deploy \
  --acme-email ops@example.com \
  --with-monitoring \
  --monitoring-domain example.com \
  --dry-run

# 修复 Traefik 的 ACME 邮箱并强制重新签发证书
zata-ops env fix --host example.com --email ops@example.com --dry-run
zata-ops env fix --host example.com --email ops@example.com
```

详细监控栈说明见 [监控栈文档](docs/architecture/monitoring-stack.md)。

### 日志 `logs`

```bash
# 跟踪容器或 systemd 日志
zata-ops logs tail --project my-app --since 1h --dry-run

# 在最近日志里搜索关键字
zata-ops logs search "ERROR" --project my-app --dry-run
```

### 终端状态面板 `dashboard`

```bash
zata-ops dashboard --project my-app
# 不接真实数据时用 mock 模式预览界面
zata-ops dashboard --mock --project my-app
```

### SSH 端口转发 `tunnel`

`tunnel` 在后台启动系统 `ssh`，并按名称管理进程。`--` 后直接传入完整
`ssh` 参数；本地转发用 `-L`，远端转发用 `-R`。

```bash
# 后台把远端 PostgreSQL 映射到本机 19000
zata-ops tunnel open db-access -- \
    ssh -N -L 127.0.0.1:19000:127.0.0.1:5432 user@bastion.example.com
zata-ops tunnel list
zata-ops tunnel status db-access
zata-ops tunnel close db-access
```

`open` 立即返回，不代表数据库已可登录；请另用数据库客户端验证本机
`127.0.0.1:19000`。需要前台运行或自动重连时直接使用 `ssh` / `autossh`。
详细说明见 [SSH 隧道指南](docs/guides/tunnel.md)。

---

## 定时备份

`zata-ops` 只提供按需触发的 `db backup` 命令,定时调度由你已有的基础设施负责,选其一即可:

- 部署机上的 **systemd timer**
- crontab:`@daily zata-ops db backup`
- **GitHub Actions** 的 `schedule:` workflow
- **Dokploy 定时任务**调用 `zata-ops`
- 维护窗口前手动执行

---

## 配置项

服务器资产、SSH 密钥引用和应用部署清单采用本机私有配置：复制
`config/servers.example.toml` 到被 Git 忽略的 `config/private/servers.toml`，
填写后运行 `just inventory-check`。详见[私有运维配置](docs/guides/private-inventory.md)。

`zata-ops` 从当前工作目录的 `.env` / `.env.local` 读取配置(优先级:CLI 参数 > `.env.local` > `.env`)。完整模板见本仓库或下游项目的 `.env.example`。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PROJECT` | `app` | 所有命令 |
| `DATABASE_URL` | — | `db backup` / `db restore` / `db migrate` |
| `S3_ENDPOINT` | — | `db *` |
| `S3_ACCESS_KEY` | — | `db *` |
| `S3_SECRET_KEY` | — | `db *` |
| `S3_BUCKET` | `app-backups` | `db *` |
| `S3_PREFIX` | `app-backups` | `db *` |
| `S3_ADDRESSING_STYLE` | `path` | `db *`(MinIO 用 `path`,阿里 OSS / 腾讯 COS 用 `virtual`,AWS S3 用 `auto`) |
| `RETENTION_DAYS` | `30` | `db backup` |
| `FULL_BACKUP_DAY` | `6`(周日,0=周一) | `db backup` |
| `BACKUP_TIME` | `18:00` | `db backup` |
| `LOGS_DIR` | `/app/backend/logs` | `db backup` / `db restore` |
| `RESOURCES_DIR` | `/app/backend/data` | `db backup` / `db restore` |
| `WORK_DIR` | `/tmp/backups` | `db backup` / `db restore` |

---

## 在下游项目里调用

`zata-ops` 设计为被下游项目的 `justfile` recipe 直接调用,例如:

```just
backup:
    zata-ops db backup

restore ts:
    zata-ops db restore --from {{ts}} --restore-db --yes
```

---

## 本地栈 `local/`

`local/` 放**只在本机运行、不部署到服务器**的开发与测试栈,目录索引与共同约定见 [`local/README.md`](local/README.md):

| 路径 | 用途 |
| --- | --- |
| `local/e2b-embed/` | 本机 E2B Embed 沙箱控制面:在 Lima VM 里运行官方 Embed 栈,给应用提供真实的 Firecracker microVM 沙箱。用法与运维见该目录的 `README.md` 与[本地 E2B Embed 沙箱](docs/guides/e2b-embed.md)。 |
| `local/docker-compose.testing.yml` | 本机测试中间件:MinIO、Redis、PostgreSQL、MySQL、Qdrant、RAGflow、Jaeger、沙箱出网代理等,模拟外部服务供本地开发/测试。用 `just testing`(`ps` / `up` / `logs` / `restart` / `recreate` / `down`,可带服务名;`e2b` 是管 E2B 栈的特殊目标)管理;固定 Compose 项目名 `zata-ops`,卷名前缀不随文件位置变化。 |

两套栈都只为本地开发/测试服务,凭据是明文测试值,不要用于生产。

---

## 开发

在 zata-ops 仓库根目录下:

```bash
uv sync
uv run pytest            # 23 个快速单测
uv run zata-ops --help   # 不安装直接跑 CLI
```

如果正在二次开发 zata-ops,希望源码改动立即生效,可以可编辑方式安装:

```bash
uv tool install -e .                      # 可编辑安装,源码改动无需重装
uv tool install -e . --force              # 已有同名工具时覆盖
uv tool install -e . --reinstall          # pyproject.toml / 依赖变更后强制重装
```

---

## 背景

过去每个下游项目都从 `zata_code_template` 复制 `scripts/backup_service/` 与 `deploy/vps-traefik/`,意味着 N 个项目维护着同一份备份逻辑,修复需要在每个下游手工同步。

`zata-ops` 把这些能力收拢到一处:**装一次,所有下游项目在 `justfile` recipe 里调用即可**。
