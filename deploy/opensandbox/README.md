# OpenSandbox + Docker + gVisor 部署包

本目录支持 Ubuntu 24.04 x86_64、Python 3.12 与 systemd 的首次私有试点部署。服务端通过 Docker 创建强制使用 gVisor `runsc` 的沙箱，不依赖 KVM。安装器默认只预览；只有显式传入 `--apply` 才会安装系统包、热加载 Docker、创建网络并启动服务。2026-10-21 已在一台 2 vCPU / 8 GiB 级别主机完成部署与真实 SDK smoke；详见部署指南的试点验证记录。

| 文件 | 用途 |
| --- | --- |
| `config.example.toml` | 固定试点配置：loopback API、gVisor、内部网络、默认数据路径 |
| `server.env.example` | API key 占位示例；真实 key 用 `just opensandbox-configure` 本机生成 |
| `docker-daemon.fragment.json` | 文档化的 runtime 片段；安装器会安全合并而不覆盖 daemon 配置 |
| `opensandbox.service` | systemd unit 模板，安装时替换数据路径 |
| `install.sh` | 目标机安装入口，转发到仓库安装器 |
| `requirements.in` / `requirements.txt` | OpenSandbox 1.1.1 服务端依赖及哈希锁 |
| `requirements-smoke.in` / `requirements-smoke.txt` | 分离的 Python SDK smoke 环境依赖及哈希锁 |
| `versions.toml` | 上游基线与 execd 多架构镜像摘要 |
| `traefik/` | 可选公网 HTTPS 入口模板；API 仍绑定 loopback，通过专用 Docker 内网和 systemd socket proxy 接入既有 Traefik |

安装器依赖 `deploy/opensandbox/` 与 `scripts/opensandbox/` 的相对目录结构；可将这两个目录单独打包并复制到其他目标机，私有配置单独传输。具体打包、权限和安装命令见 [OpenSandbox 部署指南](../../docs/guides/opensandbox.md)。脚本会记录实际 gVisor 包版本、Docker 版本、数据路径和锁文件摘要；真实服务器状态与密钥不写入 Git。带有本安装器标记的半成品可在复核后用 `--resume-partial` 继续。

首次安装默认只提供 loopback API。若目标机已有 Traefik 且需要公网 HTTPS 入口，按 [`traefik/README.md`](traefik/README.md) 添加隔离网络、systemd socket proxy 和动态路由；不要把 OpenSandbox 的 API 端口或沙箱端口直接发布到公网。

离线入口：

```bash
just opensandbox-check
just opensandbox-smoke-plan
```
