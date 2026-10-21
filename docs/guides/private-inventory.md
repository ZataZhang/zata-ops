# 私有运维配置

仓库共享工具、配置结构和验证方式，真实服务器资产、SSH 信息和应用部署记录保留在本机。

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

## 字段约定

示例文件是字段模板，`scripts/diagnostics/check_inventory.py` 是机器可执行的结构约束。

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

## 验证

```bash
just inventory-check
just inventory-check config/servers.example.toml --example
```

默认验证 TOML、字段类型、未知字段、IP 格式、端口范围、名称唯一性，以及配置文件是否
被 Git 忽略且未被跟踪。错误只报告字段位置和错误类型，不打印真实配置值。
`--example` 仅用于公共示例或临时测试文件，跳过 Git 私有性检查。
验证不连接服务器，不验证密钥存在、SSH 认证、应用在线状态或日期格式。
缺失文件和格式错误会以非零状态退出。此清单目前独立维护，现有 CLI 不会自动读取。
