# 远端运维清单与凭据

本机安装 `zata-ops`，由自部署 OpenBao 保存环境清单和凭据。显式传入 `--profile` 启用远端模式。不传 profile 时，已有本机用法仍使用私有 TOML 和 dotenv。远端访问失败直接报错。

## 建立环境

1. 按 [OpenBao 部署指南](openbao.md) 建立私有 HTTPS 入口、Raft 持久化和启用 HMAC 的审计设备。
2. 为清单与凭据分别启用 KV v2 挂载。清单条目固定使用 `<ENV>/registry`。凭据每个路径只保存字符串字段 `value`。相互独立的凭据使用独立路径。
3. 个人、Agent、工作负载和管理员各用独立身份。Agent 默认只读所属环境清单。生产管理身份要求独立管理边界与 MFA。工作负载用专用 AppRole 短期会话，按需读取明确列出的凭据路径。
4. 在工作站配置受信任的 CA，使用 OpenBao 原生登录。通过可信启动器把短期会话作为进程作用域的 `BAO_TOKEN` 传给 CLI。避免把 token 写入 profile、dotenv、脚本或命令参数。不同身份使用不同系统账户或隔离运行环境。

示例 profile 字段如下。相对证书路径以 profile 所在目录解析。

```toml
address = "https://openbao.example.invalid"
ca_file = "openbao-ca.pem"
environment = "example-dev"
inventory_mount = "ops-inventory"
credentials_mount = "ops-credentials"
```

API 策略以实际挂载名和环境为准。例如，工作负载可读所属清单和一条凭据：

```hcl
path "ops-inventory/data/example-dev/registry" {
  capabilities = ["read"]
}
path "ops-credentials/data/example-dev/example-service" {
  capabilities = ["read"]
}
```

清单发布者额外获得所属环境清单的 `create` 和 `update`。凭据维护者按独立路径授予写权限。先确认策略合并结果，避免以操作员通配策略充当日常角色。指导人工查看时使用自己的授权会话，从可信交互终端通过 `bao kv get -field=value` 查看单条。

## 发布与查询

准备一个私有目录，包含 `servers.toml`、`services.toml` 与 `deploy_targets.toml`。复用现有 TOML 结构，远端服务将 `password_env` 替换为环境相对的 `credential_ref`。引用仅允许字母、数字、下划线与连字符组成的斜杠分段。SSH 的 `identity_file` 填文件别名，由工作站在 `~/.ssh` 下解析。服务端点、备注和所有自由文本字段都应在发布前检查，避免把明文凭据写入清单。

```bash
zata-ops --profile ops.profile.toml config publish --from-dir inventory
zata-ops --profile ops.profile.toml config publish --from-dir inventory --apply --expected-version 0
zata-ops --profile ops.profile.toml service show example-service
zata-ops --profile ops.profile.toml app inspect example-app --dry-run
zata-ops --profile ops.profile.toml deploy rollback --target example-app --to example --dry-run --json
```

首次发布使用版本 0。以后从预览读取 `current_version`，带上该值应用。并发冲突或请求超时后，先重新查询当前版本再重试。回滚用 `config rollback --to-version <VERSION>` 预览，应用时也带 `--apply --expected-version <CURRENT_VERSION>`，生成一个新版本。

查询显示引用已登记。读取权限和凭据存续由实际取用确认。远端预览会读取 OpenBao，目标侧检查或派发仍由原命令的预览开关控制。远端清单入口支持服务查询、应用检查和部署命令。其他现有命令仍使用本机配置，传入远端 profile 会报错。

## 运行可信客户端

```bash
zata-ops --profile ops.profile.toml service run \
  --credential example-db:PGPASSWORD \
  --credential example-api:EXAMPLE_API_TOKEN \
  -- python -c "import os; assert os.environ.get('PGPASSWORD') and os.environ.get('EXAMPLE_API_TOKEN')"
```

每条路径独立授权和审计。全部读取成功后才启动进程。进程采用最小系统环境加请求的绑定，标准输入和输出关闭。CLI 返回子进程退出码。任意客户端可以接触并使用被交付的值，此入口仅用于可信会话和工作负载。生产目标身份由运行方通过工作负载的独立信任配置固定并验证。

## 配置不可变审计归档

先由独立归档账户创建启用了版本控制和 Object Lock 的存储桶，设默认 Compliance 保留期和到期生命周期。为归档写入身份授予指定前缀的 `PutObject`、`GetObject`、`GetObjectVersion`、`GetObjectRetention`，用于创建和确认受保护对象版本。删除、缩短保留期、改桶配置和调整生命周期由独立边界控制。归档账户与 OpenBao 主机管理账户分开。

归档命令使用标准 AWS 凭据供应链。私有配置示例：

```toml
source = "<OPENBAO_AUDIT_JSONL>"
checkpoint = "<PRIVATE_CHECKPOINT_DB>"
bucket = "<AUDIT_BUCKET>"
prefix = "openbao/example-environment"
credential_mount = "ops-credentials"
retention_days = 30
region = "<AUDIT_REGION>"
```

替代 S3 存储可设置 `endpoint_url`、`ca_file` 和 `path_style`，并先验证真实的锁定语义。使用独立、权限受限的调度账户周期运行：

```bash
zata-ops audit archive --config audit.profile.toml
```

采集器对受保护的版本核对保留时间和内容再推进本机 SQLite 检查点。断流时保留本地审计文件，恢复后从未提交位置重试。使用重命名方式轮换文件，保留轮换文件在原目录，命名为原文件名加点号后缀。确认已归档才压缩或清理。调度与监控系统应按运行失败、无周期结果或持续 `pending_bytes` 告警。设置 `credential_mount` 后，归档命令在成功提交检查点时输出该挂载的 `access_events`，只包含请求标识、路径、身份标识与结果。把此私有输出送到独立监控端，按请求标识去重，监控敏感路径读取，并按身份统计达到阈值的读取次数。独立审计查询端按请求标识、事件类型和对象版本复查原始记录。分别验证单链路断流后的补送，以及所有 OpenBao 审计设备不可写时的关闭行为。

不可变性从独立存储确认锁定对象版本后生效。本地待送窗口内仍会受到源设备丢失的影响。使用独立归档账户在隔离桶验证提前删除和缩短保留均被拒，并按版本读回。按最新对象名称查询可能受到删除标记或新版本影响。相关依据于 2026-11-26 核验。[OpenBao 审计](https://openbao.org/docs/audit/) · [S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)

## 迁移与恢复

逐条盘点现有调用方、dotenv 引用和历史备份。先做加密备份，再签发新值、验证当前服务、发布远端引用并验证实际运行，随后撤销旧值和清理活动明文副本。重试前检查已发布版本与签发方状态。记录切换和撤销，历史归档按既定保留策略处置。

在另一个故障域保管加密 Raft 快照、解封材料、恢复 SSH 入口及授权变更记录。先在隔离网络恢复并解封，重放撤销和策略变更，用新会话分别验证允许与拒绝。归档对象保留在独立的存储层，恢复 OpenBao 快照时从该层复查历史审计。生产启用前完成真实服务试点、独立告警和隔离恢复演练。

开发机可先用合成数据运行隔离测试：

```bash
ZATA_OPS_TEST_OPENBAO=1 uv run pytest tests/test_openbao_remote_live.py -q
```

## 在服务器用 Docker Compose 部署

部署由 OpenBao 和 `zata-ops` 审计归档容器组成。宿主机需要 Docker Compose；工作站用已有 CLI 通过 OpenBao 的私有入口访问清单及凭据。

1. 在部署目录按 [OpenBao 部署指南](openbao.md) 准备 TLS。已有实例沿用其原 Compose 项目名和 Raft 卷。
2. 在独立存储端准备启用了版本控制和 Compliance 留存的 S3 桶，按上节为归档身份授予指定前缀的读写权限。
3. 将 `deploy/openbao/audit-archive.example.toml` 复制为私有目录中的 `archive.toml`，填好桶、前缀、区域和留存期。保留示例里的两个容器路径。通过权限受限的 `aws-credentials` 文件提供归档身份，文件使用标准 AWS shared credentials 的 `[default]` 格式。这两个文件所在目录在仓库中默认为 `config/private/openbao-audit/`。独立部署目录可以通过 `ZATA_OPS_AUDIT_CONFIG_DIR` 指向宿主机上的私有绝对路径。
4. 在仓库根目录选择已发布的镜像版本，拉取并启动：

   ```bash
   export ZATA_OPS_IMAGE='ghcr.io/zatazhang/zata-ops:<VERSION_TAG>'
   docker compose -f deploy/openbao/compose.yaml -f deploy/openbao/compose.audit.yaml pull
   docker compose -f deploy/openbao/compose.yaml -f deploy/openbao/compose.audit.yaml up -d
   docker compose -f deploy/openbao/compose.yaml -f deploy/openbao/compose.audit.yaml logs --tail=20 audit-worker
   ```

   上面的标签占位符要替换成实际版本。若直接使用当前源码，运行 `docker build -f docker/Dockerfile -t zata-ops:local .`，保持默认镜像名，然后执行 `up -d`。归档容器只读挂载审计文件，写入独立检查点卷。默认每 30 秒执行一轮，失败后按同一间隔重试；可设置 `ZATA_OPS_AUDIT_INTERVAL_SECONDS` 调整。OpenBao 首次启动后仍需初始化并解封。

5. 从工作站执行一次已授权的取密操作。确认归档日志出现成功记录，再按请求 ID 从独立存储读回受保护版本。停止并重启归档容器后，确认原检查点仍可用。停机使用相同的 Compose 文件组合执行 `stop`，保留 Raft、审计和检查点卷。

仓库的 CLI image 工作流会在版本标签推送后分别构建和测试 `linux/amd64` 和 `linux/arm64`，再发布合并标签到 GHCR。首次发布后，按下载范围设置包可见性。若包是私有的，部署前以有读取权限的身份登录 GHCR。[GitHub 托管运行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
