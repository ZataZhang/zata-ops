# Nacos 开发与远程部署准备

## 本地开发实例

本地 Compose 配置位于 `local/nacos/compose.yaml`，固定使用 Nacos `v3.2.4`、单机模式、内置 Derby，并只启用微服务所需的配置管理与服务发现模块。该配置供开发与验证使用，不提供高可用能力。

Compose 使用本机回环地址发布端口，其他机器无法直接连接：

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:18080` | Nacos Console (宿主机 `18080` → 容器 `8080`) |
| `http://127.0.0.1:8848` | HTTP API |
| `127.0.0.1:9848` | 客户端 gRPC |

在宿主机直接运行的服务连接 `127.0.0.1:8848`。其他 Docker Compose 服务可加入 `zata-ops-nacos-dev` 网络，通过 `nacos:8848` 和 `nacos:9848` 访问；该网络不会自动附加到其他容器。

首次在新工作区启动时，复制 `.env.example` 为 `.env`，并生成独立的认证值。`.env` 已被 Git 忽略，不要把真实认证值提交到仓库。

```bash
cp local/nacos/.env.example local/nacos/.env
```

将 `NACOS_AUTH_TOKEN` 换成 Base64 编码且解码后至少 32 字节的随机值；两个 identity 值也应独立随机生成。启动命令：

```bash
docker compose --env-file local/nacos/.env -f local/nacos/compose.yaml up -d
```

首次打开 Console 时，按初始化页面为 `nacos` 管理员设置密码。应用数据保存在 Docker 命名卷 `zata-ops-nacos-dev-data` 中。停止但保留数据：

```bash
docker compose --env-file local/nacos/.env -f local/nacos/compose.yaml down
```

删除数据卷会清空本地配置和注册信息；只在确认要重置时使用 `down -v`。

应用接入时，`.env` 保留 Nacos 地址、环境对应的 Namespace ID 和访问凭据；业务配置按服务放入 Nacos，例如 `orders-api.yaml`。应用需要使用 Nacos SDK 或对应框架集成来读取配置。Namespace 用于区分 `dev`、`test`、`prod` 等环境。

## 远程部署预案

目标服务器尚未指定，因此此处只准备部署边界和检查项，不包含远程连接信息或真实凭据。实际部署时再从私有清单选择服务器，并核对实时资源、网络和现存服务。

### 部署形态

- 个人开发或临时验证可用单节点 standalone + Derby；它不具备高可用能力。
- 承载生产服务发现或关键动态配置时，采用 Nacos 集群和外置数据库，配套数据库备份与恢复演练。
- Nacos 应留在可信内部网络。Console 仅向管理网络开放；不要把服务端口直接暴露到公网。
- 若有多个 Nacos 节点，Server 与 Console 需使用一致的 server identity 和 token secret；为管理员设置独立强密码，凭据存入私有密钥管理。

### 网络与端口

以下是 Nacos 3.x 默认端口；实际防火墙规则按部署形态最小化开放：

| 端口 | 用途 | 访问范围 |
|---|---|---|
| `8080` | Console | 管理网络 |
| `8848` | HTTP API | 内部客户端、Console 或内部网关 |
| `9848` | 客户端 gRPC | 内部客户端；负载均衡时使用 TCP 转发 |
| `9849` | Server 间 gRPC | Nacos 节点互通 |
| `7848` | Raft 通信 | Nacos 节点互通 |

不要把 `9848` 按 HTTP 方式反向代理。若通过 Traefik 发布管理页面，Console 与应用 API 仍应限制在 VPN、管理网或 IP 允许列表内；gRPC 应使用 TCP 路由或内部直连。

### 开始远程部署前需要确认

1. 目标服务器的私有清单名称、CPU 架构、可用内存和磁盘空间。
2. 该实例是开发/测试还是生产用途；是否要求单机或高可用集群。
3. 生产外置数据库的地址、版本、数据库名和凭据引用，或确认先采用临时单机存储。
4. 应用访问 Nacos 的内网地址、Console 管理入口、DNS/TLS 和 VPN/防火墙范围。
5. 凭据保存位置、数据库与 Nacos 数据备份位置，以及恢复责任人。

部署前会检查端口冲突、Docker/Compose 版本、磁盘、内存、网络可达性和已有服务，并锁定 Nacos 镜像版本。远程实施后验证 Console 登录、鉴权 API、客户端 gRPC、持久化和重启恢复。

## 官方资料核验

已于 `2026-10-29` 核对 Nacos 官方资料：

- [Docker 快速开始](https://www.nacos.io/en/docs/next/quickstart/quick-start-docker/)：单机 Derby 用于测试；首次 Console 登录需要初始化管理员密码。
- [部署最佳实践](https://www.nacos.io/docs/latest/manual/admin/deployment/deployment-best-practices/)：生产环境建议集群、外置数据库、鉴权、内部网络、监控与备份。
- [官方 nacos-docker 示例](https://github.com/nacos-group/nacos-docker/blob/master/example/standalone-derby.yaml)：数据卷挂载到 `/home/nacos/data`。
