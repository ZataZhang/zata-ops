# E2B Embed 本地沙箱栈（macOS + Lima）

本目录是 zata-ops 持有的 E2B Embed 本地部署包：在 Apple Silicon Mac 上用一台 Lima
虚拟机运行官方的单机 E2B Embed 栈，为应用提供真实的 Firecracker microVM 沙箱控制面
（REST API + client-proxy + Dashboard）。

上游 Embed 只支持「有 KVM 的 Linux 主机」，不支持 macOS。本包的解法是把整台「KVM
主机」装进 Lima VM：Firecracker 需要的主机级设置（KVM/TUN、hugepages、NBD、网络规则）
全部发生在 VM 内，不触碰 macOS。应用通过 E2B 协议连进来，不是宿主 shell 或 Docker
执行后端。

面向使用者的快速上手见 `docs/guides/e2b-embed.md`；本文档是本部署包的运维参考。

## 架构与数据流

```text
macOS 宿主（Apple Silicon）
  scripts/e2b_embed.sh …      宿主入口（在 zata-ops 仓库根目录执行）
    ├─ limactl start           创建/启动 VM「zata-e2b-embed」（lima.yaml）
    ├─ limactl copy            compose.yaml、e2b-embed.env.example
    │                          → VM:/tmp/zata-ops-e2b-embed/
    └─ limactl shell -- sudo   manage.sh <action> <runtime-commit>
         │
         ▼
  Lima VM（Ubuntu 26.04 arm64，vz + 嵌套虚拟化，8 vCPU / 12 GiB / 64 GiB）
    /var/lib/zata-ops/e2b-embed/     安装目录：compose.yaml、.env、.env.local、
                                     runtime.commit、compose.override.yaml
    docker compose 项目「e2b」       api / orchestrator（Firecracker）/ client-proxy /
                                     dashboard / dashboard-api / postgres / redis /
                                     clickhouse / vector
         │
         │ Lima 端口转发（仅宿主 127.0.0.1）
         ▼
  宿主 127.0.0.1:3000（API）· :3001（Dashboard）· :3002（沙箱流量）
```

要点：

- VM 不挂载任何宿主目录（`mounts: []`），文件进 VM 只经 `limactl copy`。
- VM 内的一切变更（host-setup、fetch-artifacts、沙箱数据）都留在 VM 磁盘上，不污染 macOS。
- 宿主侧服务端口只监听 `127.0.0.1`，不出现在局域网。

## 目录内容

本目录（`local/e2b-embed/`）只放本机运行 E2B Embed 需要的文件：

| 文件 | 角色 |
| --- | --- |
| `lima.yaml` | Lima VM 定义：Ubuntu 26.04 ARM64、`vz` + 嵌套虚拟化、8 vCPU / 12 GiB / 64 GiB，首次启动安装 Docker；只把 3000/3001/3002 固定转发到宿主 `127.0.0.1`。 |
| `compose.yaml` | E2B Embed 栈定义（Compose 项目名 `e2b`），来自官方 `e2b-dev/runtime` 的 `embed/compose`，zata-ops 审阅并持有；与上游同一提交逐字节一致，差异只有文件头的来源注释。 |
| `e2b-embed.env.example` | 镜像与运行时版本清单，安装时在 VM 内落成 `.env`；同样与上游一致，仅多两行来源注释。 |
| `manage.sh` | 在 VM 内以 root 运行的管理器（`up` / `down` / `status` / `logs` / `sdk-env`）：安装清单、写 Docker 代理、生成 Compose override、驱动 `docker compose`。 |

本目录之外、同属这条链路的文件：

| 文件 | 角色 |
| --- | --- |
| `scripts/e2b_embed.sh` | 宿主入口：起 VM、检查 `/dev/kvm` 与 `/dev/net/tun`、复制清单、调用 `manage.sh`；用法即 `./scripts/e2b_embed.sh <action>`。其中的 `runtime_commit` 同时是安装闸门。 |
| `scripts/e2b_embed_configure.py` | 把官方 SDK 导出值（`E2B_API_KEY` 等）写入应用的 `.env.local`，权限 0600，不打印 key。 |
| `scripts/build_e2b_embed_app_template.py` | 从应用的模板 Dockerfile 构建本机架构模板；在临时沙箱验证目录、依赖和运行用户。 |
| `docs/guides/e2b-embed.md` | 面向使用者的 MkDocs 指南（启动、连接应用、日常命令）。 |

## 环境要求

- Apple Silicon Mac、macOS 15 或更高，支持嵌套虚拟化（官方要求 M3 或更新）。
- Lima 2.0 或更高：`brew install lima`。
- 传入应用 env 路径并自动构建模板时，宿主机需安装 `uv`：`brew install uv`，应用仓库需包含 `deploy/sandbox/Dockerfile.e2b-template`。
- 宿主机至少 12 GiB 可用内存、20 GiB 可用磁盘（VM 配置 12 GiB 内存、64 GiB 磁盘上限）。
- 首次启动需要从 Docker Hub、Google Artifact Registry 拉取容器镜像，并从 GitHub 拉取
  Firecracker 资源；受限网络见「代理」一节。
- 只能在 macOS 上由本包驱动；Intel Mac、或在虚拟机里套虚拟机都会在启动前被拒绝。

## 快速开始

在 zata-ops 仓库根目录执行：

```bash
./scripts/e2b_embed.sh up ../zata_code_template/.env.local
```

参数是应用仓库的 env 文件路径；省略时只把栈拉起来，不写应用配置。传入的路径不存在时，
会尝试从相邻的 `../zata_code_template/.env.example` 复制一份再写入。

这条命令依次做：

1. 创建或启动 Lima VM `zata-e2b-embed`（首次会装 Ubuntu、Docker，耗时较长）。
2. 检查 VM 内 `/dev/kvm`、`/dev/net/tun` 存在（缺失即报错退出）。
3. 把 `compose.yaml`、`e2b-embed.env.example` 复制到 VM 的 `/tmp/zata-ops-e2b-embed/`。
4. 以 root 在 VM 内执行 `manage.sh up <runtime-commit>`：写 Docker 守护进程代理、
   安装清单到 `/var/lib/zata-ops/e2b-embed/`、生成 `compose.override.yaml`，
   然后 `docker compose up -d --wait` 等整条流水线跑完（见「启动流水线」）。
5. 若传入应用 env 路径，从 `ready` 服务取 SDK 变量，用应用模板 Dockerfile 构建 ARM64
   本地模板，再把 SDK 变量和模板别名写进应用的 `.env.local`
   （见「SDK 变量与应用接入」）。

日常命令：

```bash
./scripts/e2b_embed.sh status    # VM 状态 + 栈内服务状态（常驻服务应全部 healthy）
./scripts/e2b_embed.sh logs      # 全栈跟随日志（tail=100 起）
./scripts/e2b_embed.sh stop      # 停 VM（等价 limactl stop），释放 12 GiB 内存；数据保留
./scripts/e2b_embed.sh down      # 只停 Compose 栈，VM 保持运行（见「生命周期」）
```

这些动作也可以走仓库的统一入口（等价于上面的脚本命令，其中 `up` 不带应用 env 路径）：
`just testing up e2b` / `ps e2b`（`status e2b` 同义）/ `logs e2b` / `down e2b`（= `stop`）。

## 启动流水线

`up` 依赖 Compose 的 `depends_on` 顺序逐段推进；一次性服务全部以退出码 0 结束，
`--wait` 一直等到 `ready` 起来——`ready` 是「栈可用」的唯一标志：

1. **主机准备**：`preflight`（磁盘、内存检查）→ `host-setup`（KVM/TUN、hugepages、
   NBD 等）→ `fetch-artifacts`（下载 kernel、Firecracker、envd、BusyBox）。
2. **存储**：`postgres`、`redis`、`clickhouse`、`vector` 起并变 healthy →
   `db-migrator`、`clickhouse-migrator` 迁移 → `seed` 写团队 API key。
3. **控制面**：`api-secrets` 生成 api 秘密 → `orchestrator`（Firecracker 宿主进程）→
   `api`（:3000）→ `dashboard-api`（:3010）→ `dashboard`（:3001）→ `client-proxy`（:3002）。
4. **收尾**：`base-template` 通过本地 API 构建 `base` 模板 → `ready` 渲染 SDK 变量并常驻。

## 服务清单

常驻服务（`docker compose ps` 可见）：

| 服务 | 作用 |
| --- | --- |
| `postgres` | 控制面数据库（团队、模板、沙箱记录）。 |
| `redis` | 团队认证等短缓存。 |
| `clickhouse` | 沙箱与模板构建日志（api 从这里读日志，本栈没有 Loki）。 |
| `vector` | 日志采集：接收 envd/api 上报，写 ClickHouse 的 `sandbox_logs`。 |
| `orchestrator` | Firecracker 宿主进程（`privileged` + host pid/cgroup/network）；启动脚本用 `nsenter` 把二进制放进 VM 的 PID 1 命名空间运行，沙箱 cgroup 在 `/sys/fs/cgroup/e2b/`。 |
| `api` | SDK 调用的 REST API（:3000）。 |
| `dashboard-api` | Dashboard 后端（:3010），与 api 共用同一管理员 token。 |
| `dashboard` | 官方 Web Dashboard（:3001），粘贴团队 API key 登录。 |
| `client-proxy` | 沙箱流量入口（:3002，按 `E2b-Sandbox-Id` 等请求头路由；:3003 健康）。 |
| `ready` | 常驻哨兵：渲染 `/run/e2b/sdk.env` 并打印；它的存在与健康代表栈可用。 |

一次性服务（跑完即退出，退出码 0 为正常）：

| 服务 | 作用 |
| --- | --- |
| `preflight` / `host-setup` / `fetch-artifacts` | 检查并准备 VM 内核、网络与 Firecracker 资源。 |
| `db-migrator` / `clickhouse-migrator` | 两个数据库的 schema 迁移。 |
| `seed` | 首次生成团队 API key（或写入 `TEAM_API_KEY` 指定值），持久化到 `seed-state` 卷。 |
| `api-secrets` | 首次生成 api 的两个秘密，持久化到 `seed-state` 卷。 |
| `base-template` | 用本地 API 构建 `base` 模板；`FORCE_REBUILD=1` 时重建。 |

Profile 服务（默认不启动）：

| 服务 | Profile | 作用 |
| --- | --- | --- |
| `smoke` | `test` | 冒烟测试：建沙箱、执行命令、读写文件、走 client-proxy。 |
| `host-teardown` | `purge` | 撤销 `host-setup` / `fetch-artifacts` 对 VM 的修改；orchestrator 在跑时拒绝执行。 |

可调旋钮（都有默认值，需要时写进 VM 内 `.env.local`）：
`HUGEPAGES`(2048)、`NBDS_MAX`(64)、`PF_MIN_FREE_GIB`(20)、`FORCE_REBUILD`(0)、
`E2B_DASHBOARD_HOST`(localhost)。

## 端口与访问面

`lima.yaml` 显式固定的三个入口（宿主 `127.0.0.1`，端口号不变）：

| 端口 | 服务 | 用途 |
| --- | --- | --- |
| 3000 | api | SDK 调用的 REST API（`GET /health` 返回 `Health check successful`）。 |
| 3001 | dashboard | 浏览器打开，粘贴团队 API key 使用。 |
| 3002 | client-proxy | 沙箱流量入口，按请求头路由。 |

VM 内其余监听端口（Lima 默认的 loopback 转发同样会把它们映射到宿主 `127.0.0.1`；
宿主上只有 `limactl` 进程监听，`lsof -nP -iTCP -sTCP:LISTEN | grep limactl` 可查）：

| 端口 | 服务 | 备注 |
| --- | --- | --- |
| 3003 | client-proxy | 健康检查端口。 |
| 3010 | dashboard-api | Dashboard 后端。 |
| 5007 / 5008 | orchestrator | 沙箱代理 / gRPC 控制面；**5008 无认证**。 |
| 5009 / 5109 | api | 内部 gRPC / edge gRPC。 |
| 5010 / 5016 / 5017 / 5018 | orchestrator | 沙箱出网代理（hyperloop、TCP 防火墙）。 |
| 6060 / 6061 | api / orchestrator | pprof。 |
| 5432 / 6379 / 9000 / 8123 / 30006 | postgres / redis / clickhouse / vector | 存储与日志采集。 |

安全提示：这些端口只应存在于宿主 loopback。`5008` 谁连上谁就拿到整个 orchestrator，
不要在宿主做任何把 loopback 映射到局域网或公网的转发；需要调试就用
`limactl shell zata-e2b-embed` 进 VM。

沙箱内部端口没有 DNS，按 client-proxy 的请求头访问：

```bash
curl -H "E2b-Sandbox-Id: $SANDBOX_ID" -H "E2b-Sandbox-Port: 8080" http://127.0.0.1:3002/
```

## 凭据与密钥

三类凭据都在首次 `up` 时按安装生成，持久化在 `seed-state` 卷里，之后 `up` 复用；
也可以在 VM 内 `.env.local` 里指定来固定：

| 变量 | 生成者 | 存放位置 | 轮换语义 |
| --- | --- | --- | --- |
| `TEAM_API_KEY` | `seed` | `seed-state:/run/e2b/team-api-key` | 改值或删文件后 `up` 即轮换；旧 key 因 api 的团队认证缓存最多再用 5 分钟。 |
| `ADMIN_TOKEN` | `api-secrets` | `seed-state:/run/e2b/api.env`（0600） | 单变量覆盖，互不影响；api 重建后生效。 |
| `SANDBOX_ACCESS_TOKEN_HASH_SEED` | `api-secrets` | 同上 | 换值会让运行中 `secure` 沙箱的 token 立即失效。 |

- `.env` 是安装时从 `e2b-embed.env.example` 覆盖写入的受管文件，不要手改。
- 本地覆盖统一写 VM 内 `/var/lib/zata-ops/e2b-embed/.env.local`（0600，默认空文件，
  `up` 不会覆盖它）。`manage.sh` 调 Compose 时同时传这两个 env 文件；注意 Compose
  不会自动读 `.env.local`，在 VM 内手动执行命令时也要带上 `--env-file .env --env-file .env.local`。
- 秘密值不会被打印；`ready` 只把 SDK 导出值写进 `/run/e2b/sdk.env`。

## SDK 变量与应用接入

`ready` 服务把本安装的四个变量渲染进 `seed-state:/run/e2b/sdk.env`，并在 key 轮换后
重新渲染。`./scripts/e2b_embed.sh up <env-file>` 会确保应用模板存在，再把连接设置写入
应用的 env 文件：

- `SANDBOX_AGENT_PROVIDER=e2b`
- `E2B_TEMPLATE_ID=<按本机模板定义生成的别名>`
- `E2B_API_KEY`（本安装的团队 key，写入后权限 0600，不打印）
- `E2B_API_URL=http://127.0.0.1:3000`
- `E2B_SANDBOX_URL=http://127.0.0.1:3002`

手动获取（VM 内）：

```bash
limactl shell zata-e2b-embed
cd /var/lib/zata-ops/e2b-embed
sudo docker compose --env-file .env --env-file .env.local exec -T ready cat /run/e2b/sdk.env
```

云端 `E2B_TEMPLATE_IMAGE` 固定为 `linux/amd64`，而本地 Lima VM 是 ARM64，不能直接复用。
本地默认通过 E2B Template SDK 解析同一份 `deploy/sandbox/Dockerfile.e2b-template`，由
Embed 构建 ARM64 模板。构建使用本地 Embed API 与固定版本的 E2B Python SDK `2.32.0`，不修改与上游
对齐的 Embed Compose 清单。模板别名根据 Dockerfile 内容生成；已有别名会复用，
并在临时沙箱验证工作目录、输出目录、依赖和非 root 用户。

## 代理（受限网络）

栈内需要出网的三类动作——拉镜像、`fetch-artifacts` 下载资源、构建模板——都可以走宿主代理：

1. 在宿主 shell 里 `export http_proxy/https_proxy/no_proxy`（小写或大写都认）。
2. 宿主入口用 `sudo --preserve-env=...` 把代理带进 VM 的 `manage.sh`。
3. `manage.sh up` 先把代理写进 VM 的 `/etc/docker/daemon.json`（有变化时重启 Docker
   守护进程），再生成 `/var/lib/zata-ops/e2b-embed/compose.override.yaml`，把代理注入
   `fetch-artifacts`、`orchestrator`、`base-template` 三个服务。
4. `NO_PROXY` 自动包含 loopback 与 `host.lima.internal`、`host.docker.internal`。

```bash
export HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890
./scripts/e2b_embed.sh up ../zata_code_template/.env.local
```

## 数据、状态与生命周期

Compose 卷（项目名 `e2b`，`down` 保留，`down -v` 删除）：

| 卷 | 内容 |
| --- | --- |
| `e2b_postgres` | 控制面数据库：团队、模板、沙箱记录。 |
| `e2b_clickhouse` | 沙箱与模板构建日志。 |
| `e2b_redis` | 只有短期缓存，丢了无碍。 |
| `e2b_seed-state` | 团队 API key、api 秘密、`ready` 的 `sdk.env`。 |

VM 卷之外的持久状态：orchestrator 经 `nsenter` 运行在 VM 的命名空间里，直接使用 VM 的
主机级路径——`/fc-versions`、`/fc-kernels`、`/fc-busybox`、`/fc-envd`、`/fc-vm`（沙箱
工作目录）、`/var/lib/e2b/storage`（模板与构建缓存）等；`down` / `down -v` 都不清理它们。

停机和清理，按破坏性从低到高：

1. `./scripts/e2b_embed.sh down`（VM 内 `compose down`）——停栈，保留全部数据。
2. `./scripts/e2b_embed.sh stop`（等价 `limactl stop zata-e2b-embed`，`just testing down e2b` 同义）——停 VM，释放 12 GiB 内存；数据仍在 VM 磁盘。
3. VM 内 `docker compose … down -v`——连卷一起删：数据库、模板记录、key、api 秘密全部消失。
   再次 `up` 会生成新 key，需要重跑 `./scripts/e2b_embed.sh up <应用 env 路径>` 更新应用。
4. VM 内 `docker compose --profile purge run --rm host-teardown`——撤销 VM 内的主机级修改；
   它在 orchestrator 运行时会拒绝，所以必须先执行第 3 步。
5. `limactl stop zata-e2b-embed && limactl delete zata-e2b-embed`——删除整台 VM 及其磁盘
   （运行中删除需 `--force`），所有本地 E2B 状态归零。

## 版本固定与升级

- 本目录整体对应官方 `e2b-dev/runtime` 仓库 `embed/compose` 目录的提交
  `0c21aa2277b59a1d040761ed3fbbb29a78775470`；`compose.yaml` 与 `e2b-embed.env.example`
  与该提交一致，差异只有文件头注释，升级时可以整文件替换再补注释。
- 镜像与版本 pin 全在 `e2b-embed.env.example`：带 `# x-release-please-version` 标记的行
  由官方发布工具随平台版本移动，其余（dashboard、envd、kernel、Firecracker、BusyBox）
  手工固定。
- 安装闸门：`scripts/e2b_embed.sh` 里固定的 `runtime_commit` 会传给 `manage.sh`，与 VM 内
  `/var/lib/zata-ops/e2b-embed/runtime.commit` 比对；不一致直接拒绝启动，防止隐式升级。
- `e2b-embed.env.example` 里的 `RUNTIME_COMMIT=` 是官方清单自带的值，随官方发布移动，
  不参与上面的闸门，不要手工改。

升级步骤：

1. 备份数据：至少归档 `e2b_postgres`、`e2b_clickhouse`、`e2b_seed-state` 三个卷
   （丢了 `seed-state` 等于换 key）。
2. 审阅上游差异：对比两个提交之间 `embed/compose` 的 `compose.yaml`、`.env` 与 `scripts/`。
3. 三处一起更新：`local/e2b-embed/compose.yaml`、`local/e2b-embed/e2b-embed.env.example`、
   `scripts/e2b_embed.sh` 的 `runtime_commit`。
4. 把 VM 内 `runtime.commit` 改成新提交（或删除该文件，让下一次 `up` 重新写入）。
5. `./scripts/e2b_embed.sh up <应用 env 路径>`，跑下面的冒烟。

## 验证与冒烟

```bash
./scripts/e2b_embed.sh status                    # 常驻服务应为 healthy，ready 为 Up
curl http://127.0.0.1:3000/health                # Health check successful
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/api/health   # 200
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3003/             # 200
```

栈内冒烟（VM 内，栈在运行时）：

```bash
limactl shell zata-e2b-embed
cd /var/lib/zata-ops/e2b-embed
sudo docker compose --env-file .env --env-file .env.local --profile test run --rm smoke
```

## 故障排查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `E2B Embed manifests were not copied from zata-ops into this Lima VM.` | 没经过宿主入口，`manage.sh` 找不到 `/tmp/zata-ops-e2b-embed/` 里的清单。 | 用 `./scripts/e2b_embed.sh up` 重跑。 |
| `neither an image nor a build context`，或 `fetch-artifacts` 失败 | `.env` 缺失或不完整；Compose 对必需变量用 `${VAR:?}` 在解析期就报错点名变量。 | 重跑 `./scripts/e2b_embed.sh up`（会重新安装清单与 `.env`）。 |
| `E2B Embed requires /dev/kvm … nested virtualization is unavailable` | 宿主没有嵌套虚拟化（Intel Mac，或在虚拟机里跑）。 | 换 Apple Silicon + macOS 15+。 |
| `api: /run/e2b/api.env is incomplete` | `seed-state` 里的秘密文件损坏。 | 按提示删掉 `/run/e2b/api.env` 后重跑 `up`，让 `api-secrets` 重新生成。 |
| `the seed left no /run/e2b/team-api-key` | `E2B_SEED_IMAGE` 太旧，不认识 `SEED_TEAM_API_KEY_FILE`。 | 修正 `e2b-embed.env.example` 里的 seed 镜像 pin。 |
| `E2B Embed was installed from a different pinned runtime commit.` | 隐式升级保护触发。 | 按「版本固定与升级」处理，先备份再改 pin。 |
| 轮换 key 后旧 key 还能用 | api 的团队认证缓存，属预期行为。 | 最长 5 分钟后旧 key 失效。 |
| 宿主 `3000/3001/3002` 被占用 | Lima 固定转发绑定失败（例如宿主已有服务占着）。 | `lsof -nP -iTCP:3001 -sTCP:LISTEN` 找占用者，停掉后重跑 `up`。 |
| 拉镜像或下载资源超时 | 网络到 Docker Hub / GAR / GitHub 不通。 | 配好代理再 `up`（见「代理」）。 |
| 沙箱创建失败 / `orchestrator` 不健康 | 主机准备环节出了问题。 | 先看一次性服务日志：`preflight`、`host-setup`、`fetch-artifacts`、`orchestrator`。 |

排查命令：

```bash
./scripts/e2b_embed.sh logs        # 全栈跟随日志
limactl shell zata-e2b-embed       # 进 VM
cd /var/lib/zata-ops/e2b-embed
sudo docker compose --env-file .env --env-file .env.local ps
sudo docker compose --env-file .env --env-file .env.local logs --tail=100 orchestrator
```

## 边界与安全

- 这是官方的单机评估形态，不是生产部署；生产形态见 E2B 官方文档。
- VM 内 `orchestrator` 以 `privileged` + host pid/cgroup/network 运行——这是 Firecracker
  主机的正常要求；风险由 Lima 边界收敛：不挂载宿主目录、宿主只暴露 loopback、VM 网络
  之外不可达。
- 不要试图在 macOS 或别的机器上直接跑本目录的 Compose：它需要 KVM，且会修改所在 Linux
  主机（内核模块、hugepages、`/etc`）。
- Dashboard 走 http、cookie 非 Secure，仅适合本机使用；只有浏览器在另一台机器上打开
  Dashboard 时才应设置 `E2B_DASHBOARD_HOST`。

## 维护约定

- 保持「官方文件原样 + 头部注释」的形态，升级时才能低成本整文件替换；本地逻辑（清单安装、
  代理注入、`.env.local`、`compose.override.yaml`）集中在 `manage.sh` 与宿主 wrapper。
- 改动使用方式（命令、端口、变量）时同步更新 `docs/guides/e2b-embed.md` 与本文档。
