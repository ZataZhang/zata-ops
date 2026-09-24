# 本地 E2B Embed 沙箱

`e2b-embed` 在本机启动 E2B 官方 Embed 运行时。应用通过本地控制面创建真实
Firecracker microVM，再使用 E2B client-proxy 和 envd 协议执行命令、读写沙箱文件。
应用连接的是 E2B 协议，不是宿主 shell 或 Docker 执行后端。

## 环境要求

- Apple Silicon Mac、macOS 15 或更高版本，以及支持嵌套虚拟化的 Apple Virtualization.framework。
- Lima 2.0 或更高版本：`brew install lima`。
- 宿主机至少 12 GiB 可用内存和 20 GiB 可用磁盘空间；本 VM 配置为 12 GiB 内存、40 GiB 磁盘。
- 首次启动需要从 Docker Hub 与 Google Artifact Registry 下载容器镜像和 Firecracker 资源。Compose 部署定义和镜像版本清单由 `zata-ops/local/e2b-embed/` 持有。

启动脚本会创建一台 Ubuntu 26.04 ARM64 Lima VM，启用嵌套虚拟化并在 VM 内安装 Docker。E2B
Embed 的 host setup 会调整该 Linux VM 的 KVM/TUN 设备、huge pages、内核模块与网络规则；
这些设置不会应用到 macOS 宿主。VM 不挂载宿主目录。端口 `3000`（控制面）、`3001`
（Dashboard）和 `3002`（沙箱代理）只转发到宿主 `127.0.0.1`。

## 启动并连接应用

在应用仓库对应的 `zata-ops` 仓库根目录执行：

```bash
./scripts/e2b_embed.sh up ../zata_code_template/.env.local
```

首次运行会把仓库内的 `compose.yaml` 和版本配置复制到 Lima VM，再启动 E2B 官方 Compose 栈、创建 `base` 模板，最后将以下设置写入目标 `.env.local`：

- `SANDBOX_AGENT_PROVIDER=e2b`
- `E2B_API_KEY`（本机 Embed team key）
- `E2B_API_URL=http://127.0.0.1:3000`
- `E2B_SANDBOX_URL=http://127.0.0.1:3002`
- `E2B_TEMPLATE_ID=base`

API key 以权限 `0600` 写入 `.env.local`，不会打印到终端。应用的 `config.toml` 只声明环境
变量名和可覆盖默认值，不存实际密钥。之后正常启动应用后端；后端会对 Embed 控制面执行
可达性探测，创建沙箱时经本地 API 控制面操作，命令和文件请求则发送至 `3002` 并附带
E2B 沙箱 ID、envd 端口和 access-token 协议头。

默认 `base` 模板用于验证控制面、命令执行和文件读写。如果应用工作流依赖额外库，需按
E2B Embed 提供的模板构建方式扩展 `base`，或创建新模板，再将 `E2B_TEMPLATE_ID` 改为模板名。

## 运维命令

```bash
./scripts/e2b_embed.sh status
./scripts/e2b_embed.sh logs
./scripts/e2b_embed.sh stop     # 停 VM，释放 12 GiB 内存；数据保留
./scripts/e2b_embed.sh down     # 只停 Compose 栈，VM 保持运行
```

也可以用仓库里的统一入口：`just testing status e2b`、`just testing up e2b`、
`just testing logs e2b`、`just testing down e2b`。

`just testing down e2b`（等价于 `./scripts/e2b_embed.sh stop`，即 `limactl stop zata-e2b-embed`）
停掉整台虚拟机、释放 12 GiB 内存；E2B 数据库、模板和 API key 都留在 VM 磁盘里，之后
`./scripts/e2b_embed.sh up` 会原地恢复。`./scripts/e2b_embed.sh down` 只停 Compose 服务、
VM 保持运行。不要使用 `docker compose down -v` 或 E2B 的 host teardown；这些会删除本地状态
或撤销 VM 中的 Firecracker 主机设置。

仓库内的 Embed Compose 栈和版本清单来自 E2B runtime 源码提交
`0c21aa2277b59a1d040761ed3fbbb29a78775470`。升级时需审阅
[E2B runtime 官方仓库](https://github.com/e2b-dev/runtime) 中的 Compose 与配置变更，更新
`local/e2b-embed/compose.yaml`、`local/e2b-embed/e2b-embed.env.example`，并同步更新
`scripts/e2b_embed.sh` 中固定的源码提交。
