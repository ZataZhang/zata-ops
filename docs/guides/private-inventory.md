# 私有运维配置

仓库共享工具、配置结构和验证方式，真实服务器资产、SSH 信息和应用部署记录保留在本机。
仓库内的配置入口与目录说明位于 `config/README.md`。

## 首次配置

安装 `uv` 和 `just` 后，在仓库根目录运行：

```bash
uv sync
mkdir -p config/private
# 仅首次执行；已有清单时不要覆盖。
cp -n config/servers.example.toml config/private/servers.toml
chmod 700 config/private
chmod 600 config/private/servers.toml
just inventory-check
```

编辑 `config/private/servers.toml`，把示例服务器与应用替换为自己的配置。
真实文件和该目录下其他私有配置均由 `.gitignore` 排除；示例文件只能包含虚构信息。
跨机器交接时通过团队的安全渠道单独传递真实配置，并将密钥路径调整为接收者的本机路径。
Git clone 不会带上真实配置。
数据库及其他服务的 URL、账号和密码管理见[服务与凭据管理](private-secrets.md)。

## 本机备份与手动复制提醒

修改已有私有配置前运行：

```bash
just private-backup
```

命令把 `config/private/` 内的文件复制为 `config/private/backups/private-<UTC时间>.tar.gz`，
不包含 `backups/` 和提醒状态文件，保留最近 20 份。备份目录权限为 `700`、文件为 `600`，
均由 Git 忽略；压缩包未加密。没有私有配置时命令失败，不会创建空备份。
本机副本用于恢复误改，手动检查压缩包并把所需文件解压到临时目录，校验后再替换正式配置。

```bash
just private-backup-reminder
```

首次运行或距上次确认满 7 天时，提示你把 `config/private/` 手动复制到自己选择的其他位置。
`just inventory-check` 校验成功后也会检查提醒；提醒不会导致校验失败。
没有后台定时任务，只有运行这些命令时才检查，因此关闭项目期间不会弹出通知。
生成本机备份不会重置提醒时间。

**完成手动复制后**运行：

```bash
just private-copy-done
```

它只记录确认时间到私有的 `.copy-reminder` 文件，不执行复制。
Agent 只有在用户明确确认已复制后才能代为执行。

## 查看配置总览

从仓库根目录运行：

```bash
just private-config-show
```

命令以 JSON 汇总当前 `config/private/` 下的服务器与应用清单、服务元数据、OpenSandbox 配置，
并列出凭据名称及配置状态。它只读取活动配置，不连接服务器、不修改文件，也不展开历史快照或备份。
API key、密码、Token 等值会隐藏；自由备注也会省略，以免备注中意外包含凭据。
OpenSandbox 的 `configuration_valid` 只表示本机文件通过离线校验，不代表远端服务此刻可用。

该总览只覆盖仓库已支持的私有运维配置格式；项目 `.env`、业务应用配置和任意私有文件不在扫描范围内。

## 字段约定

示例文件是字段模板，`src/zata_ops/inventory.py` 是共享结构约束；校验脚本与应用检查 CLI 复用同一契约。

| 字段 | 约定 |
| --- | --- |
| `schema_version` | 当前为整数 `1` |
| `updated_at` | 最近修改日期，使用 `YYYY-MM-DD` 字符串 |
| `servers` | 至少一台服务器；`name` 唯一 |
| `public_ip` / `private_ip` | IP 地址；未知时为空字符串 |
| `cpu_cores` / `memory_gb` | 正整数，内存单位 GB |
| `region` / `notes` | 地域与运维备注 |
| `ssh.user` / `ssh.port` / `ssh.identity_file` | 登录用户、1–65535 的整数端口、本机私钥路径或凭据引用；未知时为空字符串 |
| `ssh.verification_status` | `pending` 或 `verified`；后者要求 SSH 字段填写完整 |
| `applications_inventory_status` | `pending` 表示尚未盘点，`complete` 表示盘点完成 |
| `applications` | 每个部署实例一项，同一服务器上应用名称唯一；空数组不等于已确认无应用，须结合盘点状态 |
| 应用字段 | `name`、`purpose`、`url`、`ports`（整数数组）、`deployment_type`、`deployment_id`、`deployment_dir`、`notes`、`verified_at` |

无已知应用时，在对应 `[[servers]]` 字段中设置 `applications = []`，删除其示例
`[[servers.applications]]` 表。同一应用部署在多台服务器时，分别登记在各服务器下。
配置仅记录密钥路径或凭据引用，禁止保存私钥正文。新增、迁移或下线应用时同步维护清单。

## 可读格式与维护流程

服务器清单保持 TOML schema v1，采用三级视觉层次：

```toml
# 服务器 1：example-server
[[servers]]
  name = "example-server"
  # 其他服务器字段……

  # ── SSH 连接 ──
  [servers.ssh]
    user = "deploy"
    # 其他 SSH 字段……

  # ── 应用 1：example-app ──
  [[servers.applications]]
    name = "example-app"
    # 其他应用字段……
```

上面是布局片段，完整字段以 `config/servers.example.toml` 为准。服务器表不缩进，
服务器字段与 SSH/应用表缩进两格，SSH/应用字段缩进四格；每台服务器有分隔标题，
每个应用有编号。空应用数组仍写在服务器层级，不要放进 SSH 表。

长 `notes` 使用 TOML 多行基本字符串与行尾反斜杠续行，便于阅读且不向实际值加入换行。
不要手工删除续行反斜杠；工具会验证格式化前后的解析值一致。

```bash
# 默认只检查格式，不写文件、不输出真实配置
just inventory-format

# 自动备份已有私有目录后原子写入，再验证结构
just inventory-format config/private/servers.toml --write
just inventory-check

# 检查公开示例（无需 Git 私有性检查）
just inventory-format config/servers.example.toml --example
```

维护顺序：手工编辑前 `just private-backup` → 修改字段 → `inventory-format --write` →
`inventory-check`。格式化脚本自身仍会在实际写入前备份，以保护手工编辑后的版本；
格式已经一致时不写入、不额外备份，也不更新 `updated_at`。

格式检查退出码为 `0`（格式一致或写入成功）、`1`（需要格式化，未修改）、`2`（文件/结构/私有性错误）。
写入只允许 Git 忽略且未跟踪的私有文件；`--example --write` 只允许仓库自带的公开示例。
符号链接路径拒绝写入。错误不会输出真实字段值。

格式化会重新生成注释、标题与空行，原有自定义注释会被替换。必须保留的信息应存入 `notes`
或私有盘点快照。工具保留服务器和应用顺序，不改变字段值、应用状态或 SSH 验证状态。
格式化不是新增记录向导，也不会连接服务器；现有平铺格式仍能被读取与校验，不强制迁移 schema。

## 验证

```bash
just inventory-check
just inventory-check config/servers.example.toml --example
```

默认验证 TOML、字段类型、未知字段、IP 格式、端口范围、名称唯一性，以及配置文件是否
被 Git 忽略且未被跟踪。错误只报告字段位置和错误类型，不打印真实配置值。
`--example` 仅用于公共示例或临时测试文件，跳过 Git 私有性检查。
验证不连接服务器，不验证密钥存在、SSH 认证、应用在线状态或日期格式。
缺失文件和格式错误会以非零状态退出。`app inspect` 已接入此清单，其他命令尚未自动读取。

## 应用现场检查

```bash
zata-ops app inspect example-app --server example-server --dry-run
zata-ops app inspect example-app --server example-server
```

该命令独立检查 SSH、运行状态和从本机发起的 HTTP HEAD，输出带时间的 JSON 报告。
同名应用部署在多台服务器时必须用 `--server` 指定实例。详细语义、退出码和验证边界见[应用现场检查](application-inspection.md)。
