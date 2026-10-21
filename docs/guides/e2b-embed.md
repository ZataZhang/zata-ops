# 本地 E2B Embed 沙箱

`e2b-embed` 在本机启动 E2B 官方 Embed 运行时。应用通过本地控制面创建真实
Firecracker microVM，再使用 E2B client-proxy 和 envd 协议执行命令、读写沙箱文件。
应用连接的是 E2B 协议，不是宿主 shell 或 Docker 执行后端。

## 环境要求

- Apple Silicon Mac、macOS 15 或更高版本，以及支持嵌套虚拟化的 Apple Virtualization.framework。
- Lima 2.0 或更高版本：`brew install lima`。
- 传入应用 env 路径并自动构建模板时，宿主机需安装 `uv`：`brew install uv`；应用仓库需包含 `deploy/sandbox/Dockerfile.e2b-template`。
- 宿主机至少 12 GiB 可用内存和 20 GiB 可用磁盘空间；本 VM 配置为 12 GiB 内存、64 GiB 磁盘。
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

首次运行会把仓库内的 `compose.yaml` 和版本配置复制到 Lima VM，再启动 E2B 官方 Compose 栈并创建 `base` 模板。传入应用 `.env.local` 时，脚本从同一应用仓库的 `deploy/sandbox/Dockerfile.e2b-template` 构建本机架构的应用模板。模板别名由 Dockerfile 内容派生；定义变化会得到新别名，已有别名则复用并做 smoke check。随后脚本把连接配置写入目标文件：

- `SANDBOX_AGENT_PROVIDER=e2b`
- `E2B_API_KEY`（本机 Embed team key）
- `E2B_API_URL=http://127.0.0.1:3000`
- `E2B_SANDBOX_URL=http://127.0.0.1:3002`
- `E2B_TEMPLATE_ID=<按本机模板定义生成的别名>`

API key 以权限 `0600` 写入 `.env.local`，不会打印到终端。应用的 `config.toml` 只声明环境
变量名和可覆盖默认值，不存实际密钥。之后正常启动应用后端；后端会对 Embed 控制面执行
可达性探测，创建沙箱时经本地 API 控制面操作，命令和文件请求则发送至 `3002` 并附带
E2B 沙箱 ID、envd 端口和 access-token 协议头。

默认 `base` 模板用于验证控制面、命令执行和文件读写。云端脚本生成的
`E2B_TEMPLATE_IMAGE` 固定为 `linux/amd64`；本地 Lima VM 是 ARM64，所以不能直接把这张镜像
作为本地模板。默认路径通过 E2B Template SDK 解析同一份
`deploy/sandbox/Dockerfile.e2b-template`，由本地 Embed 在 ARM64 构建虚拟机中生成模板；
Compose 文件仍保持与上游一致。修改 Dockerfile 后重新运行带应用 env 路径的 `up` 命令会生成新别名并构建新模板。

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

## 故障排查

### 模板构建极慢或看似卡住（宿主代理未开 TUN）

**症状**（2026-10-08 实测）：

- `e2b_embed.sh up` 构建应用模板时在 pip 步骤长时间停滞，日志停在
  `Downloading numpy-...whl (13.9 MB)` 之类的行几分钟不动；
- 或 pip 以 `THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE`
  失败（慢速/中断的下载产生损坏的包文件）；
- Lima VM 出口带宽只有 **几十 KB/s**，而同一 Dockerfile 在宿主 Docker 里两分钟就建完。

**根因**：构建链路是 `Firecracker 构建机 → Embed 的 HTTP 代理 → Lima VM → 宿主`。宿主代理
（如 Clash）**未开启 TUN 模式/虚拟网卡**时，Lima VM 的出站流量走默认 NAT 直连，实际带宽
被压到几十 KB/s；apt 与 pip 全量下载数十 MB（numpy/scipy/pandas 的 aarch64 wheel）按此
带宽要 30 分钟以上，之后还有 rootfs 快照。整个链路没有任何「构建失败/重试」提示，日志也
不滚动——看起来像卡死，其实在极慢地下载。

**诊断**（在 Lima VM 里测速，这是构建机真正走的路径）：

```bash
limactl shell zata-e2b-embed -- curl -s -o /dev/null --max-time 15 \
  -w "%{speed_download} B/s\n" "https://speed.cloudflare.com/__down?bytes=5000000"
```

几十 KB/s = 代理未生效；MB/s 量级 = 正常（可对照宿主机同条命令）。

**修复**：开启 Clash（或等效代理）的 **TUN 模式/虚拟网卡**，然后重新运行
`e2b_embed.sh up`。构建层缓存保留，apt 层会命中缓存，pip 在快路径下几十秒完成——实测
开 TUN 后构建本体 50 秒、smoke check 通过。**改完代理无需重启 Lima VM。**

**排查时的两个提示**：

- 构建日志经 SDK 的 `on_build_logs` 打到 **stderr**，`| tail` 会缓冲到进程结束才输出；
  观察进度请把 stderr 重定向到文件再 `tail -f`。
- 失败的构建也会注册模板别名（`Template.exists` 为真）；`build_e2b_embed_app_template.py`
  会自动改用 `zata-assistant-runtime-arm64-retry-N-<hash>` 别名重建，别名累积无害。
