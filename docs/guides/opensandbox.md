# OpenSandbox + Docker + gVisor 部署指南

仓库提供固定版本配置、签名源安装脚本、主机预检查、systemd 服务和真实 SDK 冒烟脚本。安装器支持 Ubuntu 24.04 x86_64、Python 3.12、systemd 与已运行的 Docker；不会安装或替换 Docker，也不会开放公网入口。

默认数据目录是 `/var/lib/opensandbox`。有独立数据盘时，可指定 `/mnt/data/opensandbox` 或其他以 `opensandbox` 结尾的绝对路径，并通过 `--require-mountpoint` 要求目标挂载点已经挂载。安装器会检查内存、Docker 数据目录空间、数据目录空间、端口和现有服务。

## 部署范围与安全边界

目标是单机、单管理员、受控任务的私有试点。业务使用 OpenSandbox 原生 SDK；沿用本地 E2B 的应用需要自行实现后端适配，本部署包不提供 E2B HTTP 兼容层。控制面在宿主机独立 Python 虚拟环境运行，由 systemd 管理；沙箱通过 Docker 使用 gVisor `runsc`，不依赖 KVM。

截至 2026-10-21，已在一台 Ubuntu 24.04 x86_64、2 vCPU、约 7.5 GiB 内存的主机完成试点部署。验证确认服务由 systemd 管理、API 仅监听 loopback、Docker 默认 runtime 仍为 `runc`、`runsc` 可用；安装及 runtime 策略修正期间 Docker 主进程 PID 未变化，既有 5 个容器仍在运行。真实 SDK smoke 在 `--network=sandbox` 模式下创建了临时沙箱，检查 `HostConfig.Runtime=runsc`、内部网络、文件读写、命令执行和清理，全部通过。启动探测曾遇到瞬时代理 502，SDK 自动重试后通过。该结果验证的是这台主机上的固定 smoke，不代表多租户或任意代码执行已具备生产隔离能力。

- OpenSandbox 服务端与 SDK 固定为 `1.1.1`；gVisor 使用官方签名 apt 源，并记录安装时选中的精确包版本。
- 服务端与 smoke SDK 使用分开的 Python 虚拟环境及哈希锁文件，不改变系统 Python。
- API 只监听 `127.0.0.1:18080`；沙箱端口只绑定 loopback，范围 `41000–41100`。
- Docker 的 `runsc` 使用 `--network=sandbox`，让 gVisor netstack 留在沙箱网络命名空间内；不得改为 `--network=host`，该模式会降低对宿主网络的隔离。
- `secure_runtime.type = "gvisor"` 与 Docker `runsc` 是强制项，没有回退到普通 `runc` 的配置。
- Docker 默认 runtime 保持原值。安装器备份并校验 daemon 配置，再通过 systemd `reload` 发送 HUP；安装前后核对 Docker 主进程 PID、重启次数、默认 runtime 和运行中容器状态。
- Docker 配置备份保存在 `/var/backups/zata-ops/opensandbox/`；安装记录保存在 `/etc/opensandbox/deployment.json`，不含 API key。
- 安装器不改防火墙、不重启服务器、不停止现有容器、不开放 API 公网端口，也不把 `runsc` 设为默认 runtime。

API key 是管理员凭据，不能给前端、模型、沙箱或不可信用户。控制面可访问 Docker socket，等同于宿主机高权限管理入口；本配置适用于受信任的单管理员试点，不是多租户授权网关。

**内部 Docker bridge 不等于完整隔离。** `--network=sandbox` 让 gVisor 使用自己的网络栈，但 Docker bridge 上的路由仍可能允许访问同网容器或宿主 bridge 地址；loopback 绑定也不能证明同机容器都无法触达控制面。完成宿主、元数据、私网、跨沙箱和资源配额测试前，只运行固定 smoke 任务，不开放任意代码执行。公网或多租户服务需要另行设计授权、租户网络、资源准入和网络策略。

## 生成本机私有配置

先备份现有本机运维配置。新建配置必须位于被 Git 忽略的 `config/private/` 下。生成器创建 256 位 API key，不打印 key，也不会覆盖已有目录。

```bash
just private-backup
just opensandbox-configure
```

默认创建 `config/private/opensandbox/config.toml` 和 `server.env`，文件权限 `600`、目录权限 `700`。独立数据盘可按目标机另建配置：

```bash
just opensandbox-configure \
  /mnt/data/opensandbox \
  config/private/opensandbox/storage-node
```

使用独立挂载点时，先在目标机检查 `findmnt --mountpoint /mnt/data`。部署命令也可加 `--require-mountpoint /mnt/data`；如果挂载不存在，安装器会停止。

检查示例策略和真实私有配置：

```bash
just opensandbox-check
uv run python scripts/opensandbox/prepare.py validate \
  --config config/private/opensandbox/config.toml \
  --env-file config/private/opensandbox/server.env
```

部署包文件和脚本可以从本仓库复制到目标机；私有配置通过受信任的 SSH/SCP 通道单独传输，并在目标机设为 root 所有、`600` 权限。不要提交私有目录，也不要把 key 放进命令参数、聊天、终端录屏或日志。

目标机不需要完整克隆仓库，但安装器需要保留下面的目录结构。先在本机从仓库根目录打包部署脚本（不包含本机私有配置）：

```bash
just opensandbox-configure /var/lib/opensandbox config/private/opensandbox/new-server
tar --exclude='*/__pycache__' -czf /tmp/opensandbox-deploy.tar.gz \
  deploy/opensandbox scripts/opensandbox
scp /tmp/opensandbox-deploy.tar.gz user@server:/tmp/
scp config/private/opensandbox/new-server/config.toml user@server:/tmp/opensandbox-config.toml
scp config/private/opensandbox/new-server/server.env user@server:/tmp/opensandbox-server.env
```

在目标机解包并收紧私有配置权限；随后以下命令中的路径都指向这份独立部署包：

```bash
sudo mkdir -p /opt/zata-ops-deploy
sudo tar -xzf /tmp/opensandbox-deploy.tar.gz -C /opt/zata-ops-deploy
sudo install -o root -g root -m 600 /tmp/opensandbox-config.toml /etc/opensandbox-config.toml
sudo install -o root -g root -m 600 /tmp/opensandbox-server.env /etc/opensandbox-server.env
```

后续安装命令将 `./deploy/opensandbox/install.sh` 替换为 `/opt/zata-ops-deploy/deploy/opensandbox/install.sh`，并把配置参数改为 `/etc/opensandbox-config.toml` 与 `/etc/opensandbox-server.env`。Smoke 命令同样使用 `/opt/zata-ops-deploy/scripts/opensandbox/smoke.py`；安装器会把运行所需工具复制到 `/opt/opensandbox/tools`，因此安装后可删除部署包和传输暂存文件。不要把 `server.env` 留在普通用户可读目录。

## 检查部署计划

在目标机的仓库副本中，使用 root 预览安装步骤。未传 `--apply` 时只校验配置并输出计划，不访问网络、不运行 Docker 命令，也不修改主机：

```bash
sudo ./deploy/opensandbox/install.sh \
  --config config/private/opensandbox/config.toml \
  --env-file config/private/opensandbox/server.env \
  --data-root /var/lib/opensandbox
```

在目标机增加 `--check-host` 可单独运行只读主机检查；安装时 `--apply` 会再次检查：

```bash
sudo ./deploy/opensandbox/install.sh \
  --config config/private/opensandbox/config.toml \
  --env-file config/private/opensandbox/server.env \
  --data-root /var/lib/opensandbox \
  --check-host
```

独立数据盘例子：

```bash
sudo ./deploy/opensandbox/install.sh \
  --config config/private/opensandbox/storage-node/config.toml \
  --env-file config/private/opensandbox/storage-node/server.env \
  --data-root /mnt/data/opensandbox \
  --require-mountpoint /mnt/data
```

目标机还应确认：

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
findmnt --mountpoint /mnt/data
free -h
df -h / /var/lib/docker /var/lib/opensandbox
docker info --format '{{.ServerVersion}} {{.DockerRootDir}} {{.DefaultRuntime}}'
systemctl show docker -p CanReload -p MainPID -p NRestarts
ss -lnt
```

预检查要求可用内存至少 4 GiB、Docker data-root 至少剩余 10 GiB、数据目录所在文件系统至少剩余 20 GiB，且 `18080` 与 `41000–41100` 空闲。这些是安装门槛，不代表并发容量承诺。若两个目录位于同一文件系统，空间会共同消耗。2 核 / 8 GiB 级别机器建议先单任务串行试用；OpenSandbox API 没有全局并发配额，业务入口仍需自行限制。

## 在目标机执行安装

确认计划、目标身份、挂载点和空间后，在目标机显式添加 `--apply`：

```bash
sudo ./deploy/opensandbox/install.sh \
  --config config/private/opensandbox/config.toml \
  --env-file config/private/opensandbox/server.env \
  --data-root /var/lib/opensandbox \
  --apply
```

安装器会再次预检查；要求安装目录和配置目录尚不存在，避免覆盖已有部署。随后从 [gVisor 官方 apt 安装源](https://gvisor.dev/docs/user_guide/install/)下载签名密钥，添加签名仓库并安装 apt 选中的精确版本；只刷新包索引和安装需要的包，不升级系统。接着保留 Docker daemon 的其他设置、runtime 与默认 runtime，只合并 `runsc` 路径和固定参数；`dockerd --validate` 通过后热加载 Docker。最后创建内部 bridge、安装哈希锁定的服务端与 smoke SDK 虚拟环境、写入 root 私有配置和 systemd unit，并启动 loopback API。

Docker 热加载不等于重启。若主机不支持 systemd Docker reload，安装器会在写入前停止。它会核对 Docker PID、运行中容器和默认 runtime 是否保持不变。配置错误或已有 `runsc` 参数冲突时也会拒绝覆盖。安装器在 `/opt/opensandbox/.zata-ops-managed` 写入标记；某个步骤中断后，检查状态并确认标记仍属于本部署，再用相同命令加 `--resume-partial` 继续。它不会自动清理已安装的 apt 包或网络。

安装后检查：

```bash
sudo systemctl status opensandbox --no-pager
sudo /opt/opensandbox/venv/bin/python /opt/opensandbox/tools/prepare.py validate \
  --config /etc/opensandbox/config.toml \
  --env-file /etc/opensandbox/server.env \
  --data-root /var/lib/opensandbox
sudo docker --host unix:///var/run/docker.sock info \
  --format '{{.DefaultRuntime}} {{range $name, $runtime := .Runtimes}}{{$name}} {{end}}'
sudo ss -lnt
```

服务启动后不要再运行首次 `preflight` 作为健康检查，因为 API 已正常占用 `18080`。安装器会在 `/etc/opensandbox/deployment.json` 记录服务、gVisor、Docker、`runsc` 参数与锁文件摘要，不记录 key。

## 真实 SDK 冒烟验收

安装只证明服务启动和 gVisor runtime 注册。首次 smoke 会下载 execd 和工作负载镜像并实际创建、销毁一个沙箱；先评估 Docker 根盘余量。使用 Python 官方镜像并固定到本机拉取的摘要：

```bash
sudo docker pull python:3.12-slim
sudo docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
```

然后在目标机运行。将上一步输出的完整不可变镜像引用填入 `--image`：

```bash
sudo /opt/opensandbox/smoke-venv/bin/python \
  /opt/opensandbox/tools/smoke.py \
  --allow-create \
  --config /etc/opensandbox/config.toml \
  --env-file /etc/opensandbox/server.env \
  --image 'python:3.12-slim@sha256:<实际摘要>'
```

脚本创建最长 180 秒、512 MiB / 0.5 CPU 的固定任务，先核对容器身份、实际 `HostConfig.Runtime=runsc` 和内部网络，再通过 SDK 写入/读取固定文本文件并运行固定 Python 命令；最后按本次标签销毁并确认容器消失。失败时不会自动删除其他容器；清理状态不确定时要按输出的 correlation 手工核对。

通过只证明本次固定任务的创建、gVisor runtime、文本文件、命令和清理有效；不证明多租户隔离、二进制大文件、PTY、端口预览、网络阻断、容器间隔离或业务应用已接通。不要把 API key 放到沙箱镜像或 smoke 参数中。

## 业务接入与远程访问

SDK 只能由可信业务后端调用，管理员 key 不下发到前端。应用只从允许列表选择镜像/profile；入口检查任务归属、资源预算、超时和并发，并在结束后归档产物。不要把镜像、volume、entrypoint 或 runtime 扩展原样暴露给不可信调用者。

API 仍只绑定 loopback。管理工作站通过 SSH 隧道访问，不直接开放服务器公网端口：

```bash
ssh -N -L 18080:127.0.0.1:18080 user@server
```

隧道建立后，本机应用把 `http://127.0.0.1:18080` 当作 OpenSandbox 地址。下面两个变量是应用自己的约定，SDK 不会自动读取；代码要显式将 key 传给 `ConnectionConfig.api_key`：

```dotenv
OPENSANDBOX_BASE_URL=http://127.0.0.1:18080
OPENSANDBOX_API_KEY=<从可信私密配置或密钥管理器注入>
```

`OPENSANDBOX_API_KEY` 的值必须与服务端 `server.env` 中的 `OPENSANDBOX_SERVER_API_KEY` 相同。不要提交这个值，也不要把它放进前端、沙箱镜像或日志。Python SDK 固定使用已部署版本 `opensandbox==1.1.1`，连接和执行的最小示例如下；示例镜像固定到已验收的摘要：

如需从可信后端经公网访问，可按仓库 `deploy/opensandbox/traefik/README.md` 中的模板配置既有 Traefik。入口使用 TLS，OpenSandbox API 仍监听 `127.0.0.1:18080`；专用 Docker 内网中的 systemd socket proxy 转发到 loopback，不对公网发布 `18080` 或 `41000–41100`。应用配置改为：

```dotenv
OPENSANDBOX_BASE_URL=https://sandbox.example.com
OPENSANDBOX_API_KEY=<从可信私密配置或密钥管理器注入>
```

Traefik 路由不会替代 OpenSandbox API key。该 key 可管理宿主机上的沙箱控制面，只能交给可信后端；不要下发给浏览器、模型或不可信用户。OpenSandbox 1.1.1 的配置说明规定，设置 `server.api_key` 后，API 请求需携带 `OPEN-SANDBOX-API-KEY` 请求头（少数文档与健康端点除外）；参见[固定版本配置文档](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)。

在业务后端的 Python 环境中安装 SDK：

```bash
uv add 'opensandbox==1.1.1'
```

```python
import asyncio
import os
from datetime import timedelta
from urllib.parse import urlparse

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts


async def main() -> None:
    endpoint = urlparse(os.environ["OPENSANDBOX_BASE_URL"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("OPENSANDBOX_BASE_URL must be an HTTP(S) URL")

    connection = ConnectionConfig(
        domain=endpoint.netloc,
        protocol=endpoint.scheme,
        api_key=os.environ["OPENSANDBOX_API_KEY"],
        use_server_proxy=True,
        request_timeout=timedelta(seconds=30),
    )
    sandbox = await Sandbox.create(
        "python@sha256:2259debdc6af85626058a15a7bf44ad6a68b8c98c9ae82a02cf5cdb92f096d47",
        connection_config=connection,
        timeout=timedelta(seconds=180),
        ready_timeout=timedelta(seconds=60),
        resource={"cpu": "500m", "memory": "512Mi"},
        entrypoint=["python3", "-c", "import time; time.sleep(180)"],
    )
    try:
        result = await sandbox.commands.run(
            ["python3", "-c", "print('hello from OpenSandbox')"],
            opts=RunCommandOpts(timeout=timedelta(seconds=15)),
        )
        if result.exit_code != 0 or result.error is not None:
            raise RuntimeError("sandbox command failed")
        print("".join(message.text for message in result.logs.stdout))
    finally:
        try:
            await sandbox.kill()
        finally:
            await sandbox.close()


asyncio.run(main())
```

这套服务实现的是 OpenSandbox 原生 API。E2B 的 `E2B_API_URL` / `E2B_API_KEY` 不能直接指向这里：改环境变量不会转换 E2B API 请求。现有 E2B 应用需要改用 OpenSandbox SDK，或在业务端实现并维护一层 E2B 到 OpenSandbox 的适配器。

如果业务程序运行在 Docker 容器里，容器内的 `127.0.0.1` 指向该业务容器本身，不能直接用它连接宿主机 loopback API。可使用可信后端访问已配置的 HTTPS hostname，或另行设计只对私有应用网络开放的内部代理；不要为了连通性把 `18080` 暴露到公网。公开 HTTPS 入口仍是单管理员 API，不包含业务用户授权、租户隔离或资源准入；面向不可信用户前必须在可信业务后端增加这些边界。

## 停止、回退和更新

- 停止接收新任务，逐个确认沙箱结束或销毁；`systemctl stop opensandbox` 只停止控制面，不等于销毁沙箱。
- 回退 Docker 配置前先确认没有 `runsc` 容器依赖它；从 `/var/backups/zata-ops/opensandbox/` 选择变更前的 daemon 配置，恢复后运行 `dockerd --validate` 再热加载。不要在仍有 runsc 容器时删除 runtime 注册。
- 回退服务时可停止并禁用 `opensandbox.service`；不要自动删除共享网络、镜像、卷、SQLite 或工作区。
- 更新前固定新的 OpenSandbox 发布标签，重新生成服务端与 smoke 依赖哈希锁，验证配置模型和 SDK 契约，备份 SQLite 后再安排维护窗口。安装器只做首次部署，不覆盖已有部署或自动升级。
- 数据路径变更不会迁移 Docker data-root，也不会自动搬运 SQLite 或工作区；需要单独停机、复制、校验和改配置。

## 上游资料

版本与配置契约以固定发布标签为准，不能用 main 分支的新特性推断当前版本行为。

- [OpenSandbox 1.1.1 发布说明](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)
- [1.1.1 服务端配置模型](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/config.py)
- [1.1.1 Python SDK](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1/sdks/sandbox/python)
- [OpenSandbox 运行时配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- [gVisor 官方 apt 安装](https://gvisor.dev/docs/user_guide/install/)
- [gVisor 平台说明](https://gvisor.dev/docs/architecture_guide/platforms/)
- [gVisor 网络模式与网络隔离](https://gvisor.dev/docs/user_guide/networking/)

服务端依赖锁针对 Ubuntu 24.04 / Python 3.12 / Linux x86_64，smoke SDK 有独立锁文件。维护锁文件的命令：

```bash
uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes deploy/opensandbox/requirements.in \
  -o deploy/opensandbox/requirements.txt
uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes deploy/opensandbox/requirements-smoke.in \
  -o deploy/opensandbox/requirements-smoke.txt
```
