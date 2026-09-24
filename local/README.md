# local/ — 本机开发与测试栈

`local/` 放**只在本机运行、不部署到服务器**的开发与测试栈。部署到服务器的产物在 `deploy/`
（`vps-traefik/`、`monitoring/`），两者不要混放。

| 路径 | 用途 |
| --- | --- |
| `docker-compose.testing.yml` | 本地测试中间件：用容器模拟外部服务（对象存储、数据库、向量库、RAG 服务、trace 接收端、沙箱出网代理），供本仓库与下游项目本地开发/测试。 |
| `e2b-embed/` | 本机 E2B Embed 沙箱控制面：在 Lima VM 里运行官方 Embed 栈，提供真实 Firecracker microVM 沙箱。运维细节见 [`e2b-embed/README.md`](e2b-embed/README.md)，使用指南见 [`docs/guides/e2b-embed.md`](../docs/guides/e2b-embed.md)。 |

## 测试中间件 `docker-compose.testing.yml`

在仓库根目录执行：

```bash
just testing               # 查看状态
just testing up            # 启动全部（just testing up redis 只启动单个服务）
just testing logs redis    # 跟随日志（省略服务名看全部）
just testing down          # 停栈（保留卷）
```

手工方式等价于 `docker compose -f local/docker-compose.testing.yml up -d`。

服务一览（端口与连接串以文件头注释和 services 定义为准）：

| 服务 | 宿主端口 | 用途 |
| --- | --- | --- |
| minio | 9000 / 9001 | S3 兼容对象存储；Console 里需手动创建 bucket。 |
| postgres | 5432 | 通用测试数据库。 |
| mysql-app | 3306 | MySQL 测试库。 |
| redis | 6379 | 缓存 / 会话 / 队列。 |
| qdrant | 6333 / 6334 | 向量库。 |
| ragflow（含内部 mysql、infinity） | 9380 | RAG 流水线服务；两个内部依赖不发布宿主端口。 |
| jaeger | 16686 / 4317 / 4318 | 本地 OTLP trace 接收端与 UI（生产对应 ARMS）。 |
| sandbox-egress-proxy | —（仅内部网络 `zata-sandbox-egress`） | docker 档沙箱的出网 allowlist 代理，放行清单见 `deploy/sandbox/egress-proxy/squid.conf`。 |

注意事项：

- 凭据全部是明文测试值（`minioadmin`、`redis123` 之类），**不要用于生产**；端口按 `0.0.0.0`
  发布在开发机上，不要在不可信网络里长期运行。
- 文件里固定了 `name: zata-ops`：Compose 项目名决定卷前缀（`zata-ops_*`），移动文件位置时
  不要删掉它，否则会换一套空卷、旧数据像“丢失”一样。
- 本文件与模板仓库同名但内容不同（多了 mysql-app、qdrant、ragflow、jaeger、沙箱代理等），
  属项目自有文件，已在 `config.toml` 的 `project_skip_paths` 里声明，不参与模板同步。

## 沙箱控制面 `e2b-embed/`

给应用提供 E2B 协议的本地沙箱控制面（真实 Firecracker microVM）。入口：

```bash
./scripts/e2b_embed.sh up ../zata_code_template/.env.local   # 启动栈并写入应用 env
./scripts/e2b_embed.sh status                                # 查看 VM 与栈内服务
./scripts/e2b_embed.sh logs                                  # 跟随日志
./scripts/e2b_embed.sh down                                  # 停栈，保留数据
```

`just testing` 也认 `e2b` 这个特殊目标，路由到同一个脚本：

```bash
just testing up e2b        # 启动 VM 与栈，等价于 ./scripts/e2b_embed.sh up（不带应用 env 路径）
just testing ps e2b        # 查看 VM 与栈内服务（status e2b 同义）
just testing logs e2b      # 跟随栈内日志
just testing down e2b      # 停 VM，释放 12 GiB 内存，数据保留
```

需要把 SDK 变量写进应用 `.env.local` 时，仍用脚本带路径的 `up`；只想停 Compose 栈、保留
运行中的 VM（`down -v` / host-teardown 之前的那一步）时，用 `./scripts/e2b_embed.sh down`。

它只把三个端口转发到宿主 `127.0.0.1`（3000 API / 3001 Dashboard / 3002 沙箱流量），
与上面那套中间件相互独立，可以同时运行。

## 约定

- 新增「只在本机跑」的栈放这里。
- 与模板仓库同路径的文件若移动位置（如 `docker-compose.testing.yml` 从仓库根移入本目录），
  记得同步 `config.toml` 的 `project_skip_paths`，否则 `just sync-template --all` 会按相对
  路径比对、反复把模板那份当成“新增/变更”提出。
