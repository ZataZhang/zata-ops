---
name: zata-ops-operations
description: 使用 zata-ops 处理服务部署与登记、服务器与应用巡检、SSH 隧道、数据库备份/恢复/迁移、日志和 VPS 初始化，并查找 Nacos、OpenBao、OpenSandbox 及已登记服务的登录和凭据使用流程。GitHub CD 和监控接入有专项 skill。
---

# zata-ops 运维操作

帮助用户从现有 `zata-ops` CLI 和服务器工具完成操作，并报告实际验证到的结果。先读取 zata-ops 仓库自己的 `AGENTS.md` 和 `docs/ai-standards/index.md`，再按任务补读规范。从其他项目调用本技能时也沿用此入口。确认当前安装版本的 `zata-ops --help` 和相关子命令 `--help`，具体参数以当前 CLI 实现和帮助为准。

## 按任务查阅

| 任务 | 仓库权威入口 | 操作要点 |
|---|---|---|
| 服务器与应用巡检 | [私有运维配置](../../docs/guides/private-inventory.md)、[应用现场检查](../../docs/guides/application-inspection.md) | 用私有清单定位目标。应用检查用 `zata-ops app inspect <应用> --server <服务器>` 核验 SSH、运行状态和 HTTP 入口，支持 `--dry-run`。整机巡检按问题通过 SSH 只读核查磁盘、内存或系统服务，并报告核验时间。 |
| 新建、迁移与变更服务 | [部署后的登记与交付](../../docs/guides/private-secrets.md#deployment-handoff)、[私有运维配置](../../docs/guides/private-inventory.md) | 开始部署前确定凭据与恢复材料的存放方式。已授权部署或变更的收尾包含服务、账号和凭据关联登记，直接完成并验证查询及认证。交付时说明备份和恢复材料的实际保管情况。 |
| Nacos / OpenBao 生产配置迁移 | [生产迁移流程](../../docs/guides/configuration-secrets-migration.md) | 对照现有配置保留值及覆盖关系，核验实际发布与 Compose 注入、持久化网络、隔离恢复和真实业务入口。区分临时回滚材料与永久凭据，按用户约定清理。 |
| Nacos | [部署](../../docs/guides/nacos.md)、[配置读取与注入](../../docs/guides/nacos-env.md) | 确认实例和配置标识。需要确认可读性时优先列配置变量名，按指南将值注入目标进程。 |
| OpenBao | [部署与初始化](../../docs/guides/openbao.md)、[日常使用与登录](../../docs/guides/openbao-usage.md)、[FreshAI 开发者开通](../../docs/guides/openbao-developer-onboarding.md) | 新建实例同时查阅部署后的登记与交付。按部署指南配置运维与应用身份，并核验初始 root token 的处置状态。个人账号按指南登录、续期和退出，服务身份按 AppRole 指南操作。FreshAI 开发者首次开通走专项流程。 |
| OpenSandbox | [私有运维配置](../../docs/guides/private-inventory.md)、[OpenSandbox 部署与验证](../../docs/guides/opensandbox.md) | 用私有清单定位远程入口，先核验健康检查，再用调用方现有凭据验证认证。先查调用方从哪里注入凭据，再查该处字段是否存在。按部署指南运行固定 smoke，核验命令、文件和清理。 |
| 服务账号与登录入口 | [服务与凭据管理](../../docs/guides/private-secrets.md)、[Keycloak 统一登录](../../docs/guides/keycloak.md) | 在 zata-ops 仓库用 `just service-list` 或 `just service-show <名称>` 查登记入口和凭据配置状态。按服务指南登录或注入凭据，报告实际核验结果。 |
| SSH 本地/远端转发 | `docs/guides/tunnel.md`、`src/zata_ops/tunnel/cli.py` | `tunnel open <name> -- ssh ...` 后台启动；`list/status/close` 管理。认证、转发和重连由系统 `ssh` 负责。 |
| PostgreSQL 跨机迁移 | [数据库迁移](references/database-migration.md)、`src/zata_ops/db/cli.py` | 区分复制快照与应用切换，核对目标库和持续写入。 |
| S3 备份、恢复和检查 | `docs/guides/backup-and-restore.md`、`src/zata_ops/db/cli.py` | `db check` 检查 S3，**不是**数据库连通性测试；恢复参数可能替换目标数据。 |
| 私有配置备份与恢复密钥 | [为历史备份添加恢复密钥](../../docs/guides/private-inventory.md#为历史备份添加恢复密钥) | 新增恢复公钥只对新归档生效。需要用恢复私钥打开历史归档时，按指南重新加密为新副本，并用两把私钥分别运行恢复预览。 |
| VPS 初始化和 ACME 修复 | `docs/guides/vps-provisioning.md`、`src/zata_ops/env/cli.py` | 先看 `--dry-run` 的远端计划，再按用户授权执行。 |
| 容器或 systemd 日志 | `src/zata_ops/logs/cli.py` | `logs tail/search` 可先用 `--dry-run` 确认命令；输出可能含业务敏感信息。已接入 Loki 的服务器优先用 `log-query` skill 经 HTTPS 只读查询，不需要 SSH。 |
| 终端状态面板 | `src/zata_ops/observability/cli.py` | 当前 `dashboard` 使用模拟数据，不能当作真实健康检查。 |

GitHub Actions runner 主机安装和工作流调度适配用 `github-self-hosted-runner` skill；应用发布流程用 `github-vps-deploy` skill；应用日志和指标接入用 `container-observability-onboard` skill；已接入日志的集中查询用 `log-query` skill。不要把这些专项流程复制到本 skill。

## OpenBao 403 排查

1. 用调用方实际采用的代理或直连路径请求 `/v1/sys/health`。健康入口返回 `403` 时，先核对出口 IP、代理和入口白名单，或用已登记的 SSH 隧道和 CA 证书复测。
2. 入口可达后，用当前身份查 `/v1/auth/token/lookup-self`。返回 `403` 时核对 token 是否传入、是否有效，以及策略是否允许查询自身；按使用方的正常流程重新登录。
3. 用同一身份检查目标 KV 的 mount、路径和字段。身份有效但 KV 仍返回 `403` 时核对路径权限。只报告状态码和字段是否存在。先查调用方的启动配置和注入链路，再决定从哪里取凭据。

详见 [OpenBao 日常使用与故障排查](../../docs/guides/openbao-usage.md)。

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

- 查明任务是查看、部署、备份、复制、恢复、切换还是删除；只读问题不触发外部写操作。已明确授权的操作继续完成，不为例行只读检查重复询问。
- 密码、Token 和 SSH 私钥正文不得进入受 Git 跟踪的文件、命令参数、输出或日志。密码和 Token 沿用环境既有凭据来源，本机模式使用 Git 忽略的 `config/private/secrets.env`，SSH 私钥只登记路径引用。真实地址和账号保存在私有清单，可复用示例使用占位值。存放与取用方式见[服务与凭据管理](../../docs/guides/private-secrets.md)。
- 对会覆盖数据库、修改 VPS 或切换应用的步骤，先确认具体目标和现状；保留可恢复路径。快照导入成功不等于应用已切换或源库写入已同步。
- 有效期、解封责任和备份保管状态随服务登记；登记了到期时间不代表已配置提醒或自动轮换。临时网络连接应固化到实际发布定义，并按已授权范围验证重建。
- 交付时区分“命令已启动”“隧道可用”“服务认证成功”“数据核对通过”，说明实际完成到哪一步，以及留下的资源或后续切换事项。
