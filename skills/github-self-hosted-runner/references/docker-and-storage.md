# Docker 与工作目录

## 权限选择

| 需求 | 主机方案 | 核验 |
| --- | --- | --- |
| 工作流不用 Docker | 不提供 Docker socket | runner 用户无法连接生产 daemon |
| 专用构建主机，代码来源受信任 | runner 用户加入 `docker` 组 | 以 runner 用户运行 `docker info`、构建和 service container |
| 与生产容器共享主机，仍需 Docker | 独立用户的 Rootless Docker | runner 用户的 `DOCKER_HOST` 指向自己的 socket；生产 rootful daemon 保持独立 |

`docker` 组可控制主机上的 rootful daemon，效果接近主机管理员权限。Rootless Docker 降低了 runner 直接控制生产 daemon 的能力，但 PR 代码仍可消耗主机资源、访问 runner 可读的文件和网络；它不代替独立主机或临时 runner。

配置 Rootless Docker 时，按目标系统当前的 Docker 官方文档安装 `uidmap`、rootless 组件与 subordinate UID/GID 映射，并为 runner 用户启用 user systemd 服务和 linger。先以该用户验证 `docker info`、短时容器启动和端口映射，再在 runner systemd drop-in 中设置 `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`，并让 runner 在用户 Docker 服务就绪后启动。验证重启后的 socket 与 job，不要只验证当前 shell 的环境变量。

## 工作目录与挂载

优先直接把 runner 安装目录放到目标数据盘，使 `_work` 位于同一文件系统。若安装目录必须保留在 `/opt`，可用持久 bind mount 将 `_work` 映射到数据盘，并通过 systemd `RequiresMountsFor=` 约束启动顺序。不要用符号链接替代 `_work`：runner 的工作目录检查可能拒绝它。

已有工作目录切换前，先停止 runner 服务，确认没有正在执行的 job；保留原目录供回退。Docker rootless 数据目录和 runner `_work` 是两处不同的占用，需要分别核对容量。不要对共享主机做无差别 `docker system prune`。

## PostgreSQL service container

先检查主机 5432 等端口占用。宿主机型 job 可把 PostgreSQL service 映射到 `127.0.0.1` 的随机端口，再通过 GitHub Actions 的 `job.services.<name>.ports[5432]` 获取实际端口并传给数据库 URL。若使用 Rootless Docker，先用短时容器验证该端口从 runner 用户可连接，再跑真实 CI job。完成后确认测试容器已退出、生产容器未受影响。
