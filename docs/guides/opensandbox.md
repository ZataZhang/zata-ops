# OpenSandbox + Docker + gVisor 部署准备

## 当前交付状态

这是后续部署可用的配置、检查与验收材料，本轮不安装软件、不修改 Docker、不创建沙箱、不调整防火墙、不连接远端机器，也不修改本机私有服务器配置。

目标是单机、单管理员、受控任务的私有试点。业务通过 OpenSandbox 原生 SDK 接入，沿用本地 E2B 的应用需要自行实现后端适配；此包不提供 E2B HTTP 兼容层。

控制面在宿主机的独立 Python 环境运行，由 systemd 管理；它通过本机 Docker daemon 创建 gVisor 容器。这样 SDK 可以使用服务端代理，应用只需访问一个 loopback API 端口。无需 Kubernetes、宿主内核更换或 KVM。

## 基线与默认边界

- OpenSandbox 服务端和 Python SDK 基线为 `1.1.1`，上游发布标签 `release-1.1.1`。该版本修复了 1.1.0 服务端包导入缺陷。
- 服务端依赖锁针对 Ubuntu 24.04 常见的 Linux x86_64 / Python 3.12；不要直接当成其他平台的通用锁文件。
- execd 已固定 OCI 多架构清单摘要。部署时还需记录实际拉取平台、工作负载镜像摘要、runsc 版本、配置和依赖锁的校验值。
- API 只监听 `127.0.0.1:18080`，沙箱发布端口只监听 `127.0.0.1`，使用范围 `41000–41100`。
- 强制 `secure_runtime.type = "gvisor"` 与 `docker_runtime = "runsc"`，不提供普通 runc 回退配置。
- 状态和工作区位于 `/mnt/data/opensandbox/`；不在配置中填真实服务器地址或凭据。
- 使用命名内部 bridge 网络 `zata-ops-sandbox-isolated`，默认没有公网出口。镜像拉取由宿主 Docker daemon 完成，应预拉取允许的镜像；需要安装依赖的任务优先预构建镜像。

API key 是管理员凭据，不能给前端、模型、任务容器或不可信用户。控制面具有宿主 Docker 管理权限，示例 unit 以 root 运行；它不是多租户授权网关。工作区 allowed_host_paths 仅限制宿主挂载范围，不能分辨不同业务用户。

**内部网络并不等于完整的出网和租户隔离。** 同一内部 bridge 上的容器仍可能相互访问，或访问宿主 bridge 地址；loopback 绑定也不能证明所有本机容器都无法触达控制面。在专用测试节点完成私网、元数据、宿主管理端口和跨沙箱访问验证前，只运行下面的固定 smoke 任务，不开放任意代码执行。多租户或公网服务需另行落实应用授权、每租户网络/工作区隔离、资源准入和网络策略。

## 先在本机验证准备材料

```bash
just opensandbox-check
just opensandbox-smoke-plan
```

第一条仅解析示例配置和固定部署策略，输出 `example_valid` 与 `deployment_ready: false`。
第二条只列 smoke 计划，不导入 SDK、不联系平台、不调用 Docker。代码没有 SSH 或安装命令。

检查器会拒绝放宽监听地址、换成 runc、清空宿主路径允许列表、去掉镜像摘要等修改。
这是一组本仓库的策略检查，不替代 OpenSandbox 自身的配置模型；升级时两边都必须验证。
真实配置要求文件权限没有 group/other 读写执行位（建议 `600`），API key 为 64 位小写十六进制，环境文件只允许一个 `OPENSANDBOX_SERVER_API_KEY` 字段，不执行 shell 展开。

## 私有配置与未来交接

以下命令是**后续明确授权部署时执行的操作手册**，本轮仅准备文本，不执行这些写操作。

若准备修改已有 `config/private/` 内容，先运行 `just private-backup`。真实配置只放入被忽略的私有目录。

```bash
# 在运维工作站执行；已有文件不要覆盖。
mkdir -p config/private/opensandbox
cp -n deploy/opensandbox/config.example.toml config/private/opensandbox/config.toml
cp -n deploy/opensandbox/server.env.example config/private/opensandbox/server.env
chmod 700 config/private/opensandbox
chmod 600 config/private/opensandbox/config.toml config/private/opensandbox/server.env
```

使用密码管理器或本机安全编辑器，将环境文件的占位值替换为 `secrets.token_hex(32)` 生成的 key；不要把 key 放入聊天、Git、命令参数、共享终端日志或录屏。不得用示例占位 key 启动服务。

```bash
uv run python scripts/opensandbox/prepare.py validate \
  --config config/private/opensandbox/config.toml \
  --env-file config/private/opensandbox/server.env
```

复制到远端时只使用已核对身份的 SSH/SCP 或其他可信通道，保持受限权限；本轮不生成真实 key，也不复制任何文件到服务器。

## 宿主机准备顺序

### 1. 检查挂载和资源

后续部署前重新采集实际状态，不能用历史快照代替：

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
findmnt --mountpoint /mnt/data
findmnt --fstab
free -h
docker info --format '{{.DockerRootDir}}'
df -h / /mnt/data
ss -lnt
```

确认 `/mnt/data` 是预期数据盘且开机可挂载。先创建 `/mnt/data/opensandbox/control-plane` 和 `workspaces`，目录权限设为 `700`。unit 的 `RequiresMountsFor` 提供启动依赖，但不能证明该目录挂载了正确的数据盘，必须核对 findmnt。

数据盘挂载不会迁移 Docker 的镜像和容器可写层。预检查要求 Docker data-root 所在盘至少剩余 10 GiB、数据盘至少 20 GiB、当前可用内存至少 4 GiB；这些是保守的试点准入阈值，不代表并发容量承诺。根盘空间不满足时先制定清理或容量方案，不能为了通过检查自动迁移现有 Docker data-root。

### 2. 安装并固定 gVisor

在维护窗口按 [gVisor 官方安装说明](https://gvisor.dev/docs/user_guide/install/)安装适用架构的 runsc，记录版本与来源。检查实际安装路径是否为 `/usr/bin/runsc`；若不同，修改配置片段并同步检查策略。

`deploy/opensandbox/docker-daemon.fragment.json` 只是合并片段。先备份 `/etc/docker/daemon.json`，将 `runtimes.runsc` 合并进去，保留现有 runtime、代理、日志、data-root、registry mirror 和默认 runtime。不要把 runsc 改成整个宿主的 default-runtime。

片段的 `--network=host` 是 gVisor 使用所在容器网络命名空间的参数，不等同于 Docker 的 `--network=host`。沙箱 Docker 网络仍由本配置的命名 bridge 决定。`--overlay2=none` 让写入对 Docker 存储层可见，本试点仍不承诺快照或进程恢复能力。

```bash
# 仅校验配置，不会重启 Docker。
sudo dockerd --validate --config-file=/etc/docker/daemon.json
```

Docker 重启会影响已有业务，需在检查服务 restart policy、可恢复性和维护窗口后单独执行。此包不包含自动修改或重启脚本。变更后检查 `docker info` 的 runtime 注册情况，并最终通过 smoke 的实际容器 runtime 证据验证是否生效。

### 3. 创建内部网络

```bash
# 会创建宿主 Docker 网络；仅在后续授权部署时执行。
sudo docker --host unix:///var/run/docker.sock network create \
  --driver bridge --internal zata-ops-sandbox-isolated
```

网络已存在时先 inspect，确认 driver 和 Internal 属性，不能在冲突时删除重建。后续若需要公网出口，应另行设计受控代理与元数据/私网阻断，不直接将该网络改为普通 bridge 后声称仍有同样安全边界。

### 4. 安装独立控制面

建议将包文件和工具安装到以下路径，私有文件不混入源码目录：

```text
/opt/opensandbox/venv/                       独立 Python 3.12 环境
/opt/opensandbox/tools/configuration.py      本仓库共享策略校验
/opt/opensandbox/tools/prepare.py            离线检查/宿主预检查
/etc/opensandbox/config.toml                 私有 TOML，600
/etc/opensandbox/server.env                  私有 API key，600
/mnt/data/opensandbox/control-plane/         SQLite、HOME 状态
/mnt/data/opensandbox/workspaces/            允许的任务挂载区域
```

在已准备 uv 的目标机上安装锁定依赖，不改变系统 Python：

```bash
sudo uv venv --python /usr/bin/python3.12 /opt/opensandbox/venv
sudo uv pip sync --python /opt/opensandbox/venv/bin/python \
  --require-hashes deploy/opensandbox/requirements.txt
```

`sudo uv` 需要目标机 root 可访问 uv；若 uv 安装在当前用户目录，使用已核实的绝对路径，不临时运行不明来源的安装脚本。

将两个工具脚本放入 `/opt/opensandbox/tools/`，由 root 拥有且普通用户不可写。将私有配置安装到 `/etc/opensandbox/`（目录 `700`，文件 `600`），确认版本锁与目标平台一致。不要同时启动多份服务端进程使用同一个 SQLite 状态目录。

### 5. 执行只读预检查，再启动服务

在目标宿主运行下面的命令；这不是工作站远程检查器，不接受 SSH 主机参数。

```bash
sudo /opt/opensandbox/venv/bin/python /opt/opensandbox/tools/prepare.py preflight \
  --config /etc/opensandbox/config.toml \
  --env-file /etc/opensandbox/server.env
```

预检查只读 `/etc/docker/daemon.json`、挂载、内存、磁盘、监听端口与本机 Docker 元数据。Docker 调用强制指定 `unix:///var/run/docker.sock`，不使用用户的远程 Docker context。退出码：`0` 表示本轮前置检查通过，`1` 表示宿主条件未满足，`2` 表示配置非法。它不拉镜像、不创建网络/容器，也不能证明 gVisor 已成功运行任务。

端口空闲检查适用于首次启动前；服务启动后占用 18080 是正常现象，不能把这条检查当作运行中健康检查。

通过后，将 unit 安装到 `/etc/systemd/system/opensandbox.service`：

```bash
# 以下会改变服务器状态，保留给后续部署执行。
sudo systemctl daemon-reload
sudo systemctl enable --now opensandbox
sudo systemctl status opensandbox --no-pager
```

不要仅凭 unit active 宣布沙箱可用。确认 API 和动态端口没有公网监听，再执行真实 smoke。

## 真实 smoke：显式允许后才创建沙箱

先从可信来源预拉取一个包含 Python 3 的 Linux 工作负载镜像，记录其不可变 sha256 摘要。首次拉取 execd 和工作负载镜像需要考虑 Docker 根盘容量；内部网络上的容器无需公网出口。

SDK 验收脚本使用 PEP 723 声明固定 `opensandbox==1.1.1`，不把 SDK 加到 zata-ops 运行时依赖。默认仅输出计划：

```bash
uv run python scripts/opensandbox/smoke.py
```

部署获准且平台启动后，才在目标宿主运行：

```bash
# 用实际批准的镜像摘要替换 IMAGE_DIGEST；占位值不会通过参数检查。
sudo uv run --script scripts/opensandbox/smoke.py \
  --allow-create \
  --config /etc/opensandbox/config.toml \
  --env-file /etc/opensandbox/server.env \
  --image 'python@sha256:IMAGE_DIGEST'
```

脚本只连接本机 `127.0.0.1:18080`，通过 `use_server_proxy=True` 走控制面代理，创建最长 180 秒、512 MiB/0.5 CPU 的固定任务。按以下顺序检查：

1. SDK 创建实例，记录唯一 correlation 与 sandbox ID。
2. 按本次独有标签在本机 Docker 查找实例，核对平台实例 ID、实际 `HostConfig.Runtime=runsc` 和内部网络；发现降级时拒绝后续命令与文件操作。
3. 通过 SDK 写入、读取固定文件，再运行固定 Python argv 读取文件；核对内容与退出码。
4. 在异常路径也尝试 `kill` 和关闭客户端，再从本机 Docker 查询是否还有本次标签的容器。

主流程等待预算 150 秒；触发超时后还会尝试清理，因此总耗时可能更长。创建超时、SDK 连接中断或进程被强制终止时，不能保证当场回收；报告 `unverified`/`pending` 并保留最先输出的 correlation，需人工对账与验证 TTL 回收。脚本不会自动 `docker rm` 或清除其他实例，SDK close 也不能代替实例销毁。

成功只证明这一条固定任务的创建、gVisor runtime、SDK 命令/文本文件和容器清理。它不证明二进制大文件、PTY、内存快照、异常进程树取消、多租户隔离、端口预览、网络阻断或业务应用已接通。

## 业务接入与权限范围

SDK 必须由可信业务后端调用，管理员 key 不下发给前端。应用只从服务端允许列表中选择镜像/profile；执行前检查任务归属、资源预算和超时，销毁后确认产物已进入业务存储。

创建接口的镜像、volume、entrypoint、runtime 扩展不能原样暴露给不可信调用者。`max_sandbox_timeout_seconds` 只限制创建时长，不是可靠的总生存期或全局并发限制；客户端续期、创建速率与总资源配额需由可信业务入口额外限制。单个 smoke 的 resource 字段不代表服务端已经对所有调用强制 512 MiB/0.5 CPU。

远程应用访问时优先先用 SSH 隧道，不在本轮开放 API 公网入口。只有完成业务鉴权、TLS、租户路由与端口代理设计后，才另行配置公网反向代理。OpenSandbox API 与 E2B 不同，不能只替换 E2B_API_URL 后宣布迁移完成。

## 停止、回退与剩余资源

- 停止接收业务新任务，再按实际实例清单等待结束或逐个销毁，记录清理结果。
- `systemctl stop opensandbox` 只停止控制面，不证明已销毁 Docker 沙箱；仍需核对实例、卷和 TTL 状态。
- 保留 `/etc/opensandbox`、版本锁与数据目录的受限备份。升级前停止控制面并备份 SQLite，不能只复制一个可能仍有 WAL 写入的数据库文件；降级前检查 schema 兼容性。
- 回退 Docker 配置时先确认没有 runsc 实例使用它，再恢复变更前的 daemon.json，并安排维护窗口。此包不会删除共享网络、镜像、卷或其他业务资源。

## 部署后的验收清单

- [ ] 重新确认目标身份、数据盘挂载、Docker 根盘余量、内存与端口占用。
- [ ] 记录 runsc 版本、包来源和实际 systrap 参数；宿主 runtime 注册及实际容器 runtime 都正确。
- [ ] 私有 key、监听地址、文件权限与固定镜像/包版本通过校验。
- [ ] SDK 创建、文本文件、命令结果和本次容器清理通过真实 smoke。
- [ ] API、动态沙箱端口均未暴露到公网；通过预期的应用链路访问。
- [ ] 测试宿主、元数据、私网与跨沙箱流量，确定是否需要独立节点或额外网络策略。
- [ ] 覆盖应用重启重连、错误处理、任务取消、配额、产物归档和孤儿资源对账。
- [ ] 验证机器重启后的数据挂载、控制面恢复和残留实例处理。

以上条目目前均未作为远端实测通过，不应提前勾选。

## 上游与更新

资料核对日期：2026-10-21。配置和 SDK 契约优先对照固定发布标签，不以 main 分支的新特性推断当前版本可用。

- [OpenSandbox 1.1.1 发布说明](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)
- [1.1.1 服务端配置模型](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/config.py)
- [1.1.1 SDK](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1/sdks/sandbox/python)
- [运行时配置说明](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- [gVisor 平台说明](https://gvisor.dev/docs/architecture_guide/platforms/)

更新依赖锁的命令（仅维护者在审阅版本后执行）：

```bash
uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes deploy/opensandbox/requirements.in \
  -o deploy/opensandbox/requirements.txt
```

升级需重新运行离线校验、上游配置模型解析、SDK 契约检查与实际沙箱 smoke，并在私有部署记录里更新主机 runtime 和镜像证据。
