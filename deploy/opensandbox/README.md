# OpenSandbox + Docker + gVisor 部署准备包

状态：**准备完成不代表已部署，也不代表已通过真实沙箱验收**。本目录没有自动安装、启动、SSH 或修改 Docker 的脚本。

面向 Linux x86_64 / Ubuntu 24.04 / Python 3.12 的单机私有试点。控制面使用独立 Python 环境，由 systemd 管理；它通过本机 Docker socket 创建强制使用 `runsc` 的沙箱。不依赖 KVM，gVisor 使用 `systrap`。

## 文件

| 文件 | 用途 |
| --- | --- |
| `config.example.toml` | 固定的私有试点配置：loopback API、gVisor、内部网络、数据盘目录 |
| `server.env.example` | API key 占位示例；真实 key 仅进入私有配置 |
| `docker-daemon.fragment.json` | 需要人工合并的 runtime 配置片段，不能覆盖已有 daemon.json |
| `opensandbox.service` | 未来部署时安装的 systemd unit；目前没有安装 |
| `requirements.in` / `requirements.txt` | 1.1.1 服务端及 Ubuntu/Python 3.12 依赖、哈希锁定 |
| `versions.toml` | 上游基线、SDK 与 execd 镜像摘要；目标主机的 runsc 版本待核验 |

从仓库根目录运行：

```bash
just opensandbox-check
just opensandbox-smoke-plan
```

两个入口都不会访问服务器、启动 Docker 容器或创建沙箱。
完整部署步骤、限制、预检查、验收与回退见[OpenSandbox 部署指南](../../docs/guides/opensandbox.md)。
