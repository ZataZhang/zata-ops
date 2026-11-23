# OpenBao 日常使用指南

本页面向开发者和运维人员，说明个人登录、权限、密钥更新、机器接入和故障排查。首次安装与恢复见 [部署与维护](openbao.md)，公网入口见 [Traefik 配置](openbao-traefik.md)。

真实地址、账号和权限核验记录保存在本机 `config/private/openbao/` 下对应实例目录。先从 [私有运维清单](private-inventory.md) 定位实例，再阅读其 `README.md` 和 `accounts.md`。这些文件由 Git 忽略，克隆仓库后需要通过团队的安全渠道取得。

## 账号、策略和 token 的关系

开发者用个人账号登录，OpenBao 签发 token。应用携带 token 请求密钥时，OpenBao 根据策略检查路径和操作。一次登录可以供多次启动使用。

| 对象 | 作用 | 示例用途 |
| --- | --- | --- |
| Userpass 用户 | 用户名和密码登录 | 每位开发者一个账号 |
| Entity / Group | 关联身份和组织成员 | 多种登录方式映射到同一个人，按组授权 |
| Policy | 授予路径上的操作权限 | 读取某应用开发环境的密钥 |
| Token | 登录后用于请求 API 的凭据 | 缓存在开发者本机，到期后重新登录 |
| AppRole | 机器认证的角色 | CI、部署服务或后台任务使用 |
| SecretID | AppRole 登录凭据之一 | 与 RoleID 配合换取 token |

账号、SecretID 和 token 各有独立的生命周期。一次性 SecretID 用于一次登录；登录得到的 token 可以在自身权限和有效期内继续使用。具体参数以实例记录为准。[Userpass](https://openbao.org/docs/auth/userpass/) · [Identity](https://openbao.org/docs/concepts/identity/) · [AppRole](https://openbao.org/docs/auth/approle/)

## 连接实例

从私有记录取得 `BAO_ADDR`。使用 SSH 隧道时，先按实例说明打开隧道，并指定对应的公开 CA 或服务端信任证书。使用公网 HTTPS 入口时，确认当前出口在允许列表中，代理出口与直连出口可能不同。

在当前终端设置连接参数。以下占位符需要替换为实例记录中的值。

```bash
export BAO_ADDR='<OPENBAO_URL>'
export BAO_TOKEN_PATH="$HOME/.config/example-app/openbao-dev-token"
mkdir -p "$(dirname "$BAO_TOKEN_PATH")"
chmod 700 "$(dirname "$BAO_TOKEN_PATH")"
```

私有证书连接还需要 `export BAO_CACERT='<TRUSTED_CERT_PATH>'`。切换到公共 CA 签发的入口时，核对并移除不再适用的 `BAO_CACERT`。保留证书校验。

命令行会读取 shell 环境变量。项目的 `.env.local` 是否加载、如何覆盖 shell 值，取决于该项目启动器。登录和启动必须指向同一个实例及 token 文件。

## 个人登录与本地开发

使用自己的用户名，密码在终端隐藏输入。

```bash
bao login -no-print -method=userpass username='<YOUR_USERNAME>'
chmod 600 "$BAO_TOKEN_PATH"
```

`-no-print` 会缓存 token 并抑制 token 输出。缓存目录应位于仓库外。账号密码放入个人密码管理器；团队成员分别持有自己的登录凭据。[登录命令](https://openbao.org/docs/commands/login/) · [Token helper](https://openbao.org/docs/commands/token-helper/)

采用 FreshAI 团队启动器的项目可以执行以下入口。连接参数由该项目 `.env.local` 提供，具体变量与覆盖规则见项目自身的团队开发指南。

```bash
just dev-login '<YOUR_USERNAME>'
just run
```

启动器使用缓存的 token 获取开发密钥，再启动应用。本机数据库连接串可以保留为个人覆盖。Nacos 提供共享业务配置，OpenBao 提供密钥；团队需要同步维护两端的环境和路径对应关系。

## 有效期、续期和退出

| 配置 | 含义 |
| --- | --- |
| `token_ttl` | 新签发或续期时申请的有效时长 |
| `token_max_ttl` | 认证方式设置的生命周期上限，实际还受服务端配置约束 |
| `token_explicit_max_ttl` | 签发时编码到 token 的硬性最大时长 |
| `renewable` | 当前 token 是否允许续期 |

例如 TTL 为一天、最大有效期为七天，使用者需要在到期前续期才能继续使用，到达最大时长后重新登录。只有配置 TTL 不会自动续期。已经过期的 token 需要通过登录换新。[Token 生命周期](https://openbao.org/docs/concepts/tokens/)

以下命令只显示状态字段。完整的 `bao token lookup` 输出可能包含 token 本身，分享诊断时应筛选输出。

```bash
bao token lookup -format=json | jq '.data | {display_name, policies, ttl, renewable, expire_time}'
bao token renew -format=json | jq '.auth | {lease_duration, renewable}'
```

若 shell 设置了 `BAO_TOKEN` 或兼容的 `VAULT_TOKEN`，CLI 可能使用环境中的 token；排查时同时核对环境注入和文件缓存。应用仅在启动时获取密钥的情况下，token 到期影响下一次获取操作。撤销 OpenBao token 后，已经被应用读取的外部 API Key 仍需在对应服务商处轮换。

结束当前身份的访问时，可以执行 `bao token revoke -self` 撤销当前 token。撤销后再次访问需要重新登录，关联租约和子 token 也可能受影响。[Token API](https://openbao.org/docs/api/auth/token/)

## 权限如何划分

策略默认拒绝，按 API 路径和操作授权。下面是一个虚构应用的开发只读策略。

```hcl
path "kv/data/dev/example-app/runtime" {
  capabilities = ["read"]
}
```

| 操作 | 常用 capability |
| --- | --- |
| 读取内容 | `read` |
| 列出路径 | `list` |
| 首次写入 | `create` |
| 覆盖已有内容 | `update` |
| 局部修改 | `patch` |
| 删除 | `delete` |

KV v2 的内容读取走 `kv/data/...`，目录列举和版本信息走 `kv/metadata/...`。只读某条记录不一定能在 UI 中浏览整个目录。需要浏览时，由管理员补充必要的 metadata 列举权限，目录名本身也可能包含敏感信息。

读取一条 KV 记录会取得其中所有字段。按模型、沙箱或环境分别授权时，应分成不同路径。可使用 `bao token capabilities kv/data/dev/example-app/runtime` 检查当前身份对某条路径的权限。[ACL 策略](https://openbao.org/docs/concepts/policies/)

## 管理员创建个人账号

以下步骤需要能够写目标 ACL 和目标 Userpass 用户的管理身份。先检查能力；当前仓库提供的运维策略允许管理应用 ACL/AppRole，直接管理 Userpass 用户需要额外授权。实例实际授权范围以私有清单为准。

```bash
bao token capabilities sys/policies/acl/app-example-dev-read
bao token capabilities auth/userpass/users/dev_user
```

先把上节策略保存为审核过的策略文件，运行 `bao policy write app-example-dev-read '<POLICY_FILE>'`。再用 Bash 隐藏输入新用户密码，通过 stdin 传递，创建个人账号。下面的用户名和应用名为虚构示例。

```bash
read -r -s -p 'New user password: ' user_password
printf '\n'
printf '%s' "$user_password" | bao write auth/userpass/users/dev_user \
  password=- token_policies=app-example-dev-read \
  token_ttl=24h token_max_ttl=168h token_explicit_max_ttl=168h
unset user_password
```

创建前先读取目标用户，确认是否已有账号，避免覆盖既有设置。完成后，在独立 token 缓存中验证个人登录、开发路径读取和越权拒绝，再交付账号。[Userpass 管理 API](https://openbao.org/docs/api/auth/userpass/)

允许用户修改本人密码时，额外绑定仅覆盖本人密码接口的策略。

```hcl
path "auth/userpass/users/dev_user/password" {
  capabilities = ["update"]
}
```

用户登录后，可以用同样的隐藏输入方式，将新密码通过 `password=-` 写入本人密码接口。管理员重置密码、删除用户、撤销已签发 token 是需要分别处理的操作。离职或疑似泄露时，应阻止继续登录，并通过有权限的管理身份撤销相关 token；只删除用户无法证明历史 token 已全部失效。

查看账号和角色需要相应的读取或列举权限。

```bash
bao list auth/userpass/users
bao read auth/userpass/users/dev_user
bao list auth/approle/role
bao read auth/approle/role/app-example-deploy
bao policy read app-example-dev-read
```

token accessor 可用于关联、查询和撤销 token。全量列举 accessor 需要额外的 `sudo` 能力，交由有该权限的管理员操作；保存的盘点材料应避开 token、密码和 SecretID 正文。[Token accessor API](https://openbao.org/docs/api/auth/token/)

## 密钥读取、修改和轮换

Web UI 中进入相应 KV mount 和路径，即可按权限查看或创建记录。CLI 查看密钥会把内容输出到终端，应在可信终端中操作。

```bash
bao kv get -mount=kv dev/example-app/runtime
bao kv metadata get -mount=kv dev/example-app/runtime
```

更新前确认环境和当前版本。`kv put` 写入整条记录，遗漏的字段会从新版本中消失；`kv patch` 用于保留其他字段的局部修改。准备好的私有 JSON 文件可以经 stdin 提交，实际值避免直接放在命令参数中。

```bash
bao kv patch -mount=kv dev/example-app/runtime - < '<PRIVATE_PATCH_JSON>'
```

多人同时编辑时使用 CAS 指定读取时的版本，或在服务端启用 CAS 要求，避免覆盖他人更新。KV v2 区分版本软删除和永久销毁，执行前核对目标版本与恢复条件。[KV v2](https://openbao.org/docs/secrets/kv/kv-v2/)

外部密钥轮换顺序为生成新密钥、写入 OpenBao、重启或重新部署消费者、验证真实调用，最后撤销旧密钥。数据库密码还需要在数据库端同步修改；仅修改 OpenBao 中的值不会改变数据库账号密码。

## 部署服务和 AppRole

每个应用、环境设置独立机器身份，策略限定到它使用的路径。AppRole 的 RoleID 和 SecretID 用于登录，得到的 token 再用于读取密钥。SecretID 的使用次数和有效期、token 的有效期都应单独登记。[AppRole](https://openbao.org/docs/auth/approle/)

Dokploy Provider 使用其配置的 token 读取密钥。应核对当前版本支持的认证和续期能力；使用静态 token 时，需要维护换新流程。轮换 Provider token 后，应测试一次实际密钥读取或部署，再撤销旧 token。变更 OpenBao 的 KV 值后，已运行应用何时取得新值取决于它的加载方式。网络与部署接入步骤见 [Traefik 入口](openbao-traefik.md)。

## 审计与故障排查

当前部署模板将审计日志写入独立持久化卷。审计中可以核对请求时间、路径、操作、身份和错误；敏感字符串通常以 HMAC 记录。`sys/health` 等少数接口不进入这套审计，入口代理拦截的请求也不会到达 OpenBao。审计是事件记录，告警需要另行配置采集、规则和通知渠道。[审计说明](https://openbao.org/docs/audit/)

| 现象 | 优先检查 |
| --- | --- |
| 连接拒绝或超时 | 地址、SSH 隧道、DNS、代理、服务监听 |
| TLS 校验失败 | 证书信任链、域名匹配、过期时间、连接目标 |
| 健康接口返回 403 | 入口来源限制、当前出口 IP；健康检查本身无需登录 |
| 健康接口成功，读取密钥返回 403 | token 是否发送、是否到期、策略是否覆盖该 API 路径 |
| `lookup-self` 返回 403 | token 失效或缺少查询自身权限，结合审计与既有策略核验 |
| 有权读取却返回 404 | mount、路径、环境、版本是否被删除 |
| UI 看不到目录，但指定路径能读 | metadata 的列举权限和 UI 路由是否开放 |
| 连续登录失败 | 用户名、密码和 Userpass 锁定状态 |
| 新密钥已保存，应用仍用旧值 | 消费者缓存、启动加载方式、是否已重新部署 |
| 已解封服务突然不可用 | 容器状态、磁盘、审计写入失败、服务日志 |

排查 403 时先验证网络入口，再验证身份和授权，最后确认记录存在。只有 HTTP 状态不足以确定 token 到期，账号创建和过期时间应以服务端数据或审计为依据。备份、解封和灾难恢复继续参考 [部署与维护](openbao.md)。

本页依据 OpenBao 2.7.x 官方资料于 2026-11-22 核验。实例状态、账号范围及证据时间以对应私有记录为准。
