# VPS + Traefik 部署

本目录是模板衍生项目的一条**可选**部署路径。
默认的生产部署方式仍是 Dokploy，见 `docs/guides/deployment.md`。

当你自己管理一台普通的 Ubuntu/Debian VPS，并且希望满足以下需求时，使用这套方案：

- 在宿主机上安装 Docker Engine 和 Compose 插件。
- 宿主机级 Traefik 网关，挂在外部 Docker 网络上，自动签发 HTTPS 证书。
- 使用类似 `/opt/apps/zata-ops` 的应用目录。
- 通过 GitHub Actions 或本地 SSH 部署，按不可变镜像 tag 更新。

## 整体架构

```text
浏览器
   │ 80/443
   ▼
宿主机 Traefik 容器（install-docker-traefik.sh 安装，外部网络 "traefik"）
   │ 按域名路由
   ▼
应用容器（/opt/apps/<slug>/docker-compose.yml，由 deploy 用户管理）
   ├── backend    (FastAPI)
   ├── frontend   (静态站点)
   └── backup     (可选 profile)
```

职责划分：

- **Traefik（宿主机级）**：监听 80/443，负责域名路由和 Let's Encrypt 证书，与应用相互独立。
- **deploy 用户**：无 root 权限的部署专用账号，只在 docker 组里，CI/CD 通过它的 SSH key 登录执行 `docker compose`。
- **两个 env 文件**：`.env` 放部署元数据（域名、镜像引用，CI 会改写），`app.env` 放运行时密钥（只有服务器上有，CI 永不触碰）。

## 前置条件

开始之前，确认：

- 一台 Ubuntu/Debian VPS，你有 root 或免密 sudo 权限的账号。
- 域名 DNS 已解析到这台服务器（A 记录）。
- 服务器 80、443 端口对外开放（Let's Encrypt 签证书需要 80）。
- 一个容器镜像仓库（Docker Hub、GHCR、阿里云 ACR 等），用来推送应用镜像。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `install-docker-traefik.sh` | 安装 Docker Engine、Compose 插件和宿主机级 Traefik。 |
| `fix-acme-email.sh` | 修复使用了占位 Let's Encrypt 邮箱的 Traefik 安装。 |
| `bootstrap.sh` | 准备部署用户、SSH key、应用目录、compose 文件和初始 env 文件。 |
| `docker-compose.yml` | 生产应用 compose 文件，挂在外部 Traefik 网络之后。 |
| `.env.example` | 部署元数据模板：域名、网络、镜像引用。 |
| `app.env.example` | 运行时配置与密钥模板。 |
| `github-actions-deploy.yml.example` | 可选的「构建-推送-SSH 部署」工作流示例。 |

## 第一步：配置服务器（装 Docker + Traefik）

在 VPS 上以 root 或具备 sudo 权限的用户执行：

```bash
# 默认占位邮箱，拿到域名并解析到本机后即可自动签发
bash deploy/vps-traefik/install-docker-traefik.sh

# 换成自己的邮箱
ACME_EMAIL=you@your-domain.com bash deploy/vps-traefik/install-docker-traefik.sh

# 完全不启用 ACME，只提供 HTTP
ACME_EMAIL= bash deploy/vps-traefik/install-docker-traefik.sh
```

安装脚本是幂等的。它默认创建名为 `traefik` 的外部 Docker 网络，并总是写入
`letsencrypt` resolver：`ACME_EMAIL` 未设置时使用内置占位邮箱
`example@zata-ops.com`。这个地址只填充 ACME 账户的联系人字段，Let's Encrypt 不校验它，
也不影响证书签发（官方自 2025 年起已停止发送到期提醒邮件）。只有显式传
`ACME_EMAIL=` 才会跳过 `certificatesResolvers`，退化为纯 HTTP。

注意：`ACME_EMAIL` 非空时脚本会把 HTTP 全局跳转到 HTTPS。用 IP 直接访问、暂时没有
域名的场景，如果不想被跳到自签证书，显式加 `ENABLE_HTTPS_REDIRECT=false`。

安装时还会配置持久化的 `DOCKER-USER` 规则：外部接口只允许访问 Docker 发布的 TCP 80/443，
其他发布端口会被丢弃；容器访问 `169.254.169.254` 会被丢弃，宿主机上的非 root 进程访问该
元数据地址会被拒绝。规则由 `zata-docker-user-firewall.service` 在 Docker 启动后应用；
回滚时先运行 `sudo systemctl disable --now zata-docker-user-firewall.service`，再运行
`sudo /usr/local/sbin/zata-docker-user-firewall --remove`，最后删除 unit 和脚本并执行
`sudo systemctl daemon-reload`。

如果服务器上已有 Traefik，但浏览器显示的是 Traefik 默认证书，
说明 ACME 邮箱是占位符，在服务器上执行修复：

```bash
sudo bash fix-acme-email.sh --email you@your-domain.com
```

## 第二步：应用引导（bootstrap.sh 做了什么）

`bootstrap.sh` 在**本地机器**上执行，通过 SSH 把服务器准备成可以部署应用的状态。
它是交互式的（会逐步询问并确认），重复执行是安全的——已存在的用户、
SSH key、`.env`、`app.env` 都不会被覆盖。

它依次做五件事：

1. **收集输入**：服务器地址、管理账号、域名，以及应用 slug 等高级选项（回车用默认值）。
2. **生成 CD 专用 SSH key**：在本地 `~/.ssh/cd-<slug>` 生成一对 ed25519 key（已存在则复用），专供 CI/CD 登录使用。
3. **服务器初始化**：SSH 到服务器后——
   - 检查 Traefik 网络和 ACME 邮箱配置是否正常（有问题会警告并让你确认）；
   - 创建无密码的 `deploy` 用户并加入 `docker` 组；
   - 把第 2 步的公钥写入 `deploy` 用户的 `authorized_keys`；
   - 创建应用目录 `/opt/apps/<slug>` 并归属 `deploy` 用户。
4. **验证**：用新生成的 key 以 `deploy` 用户登录，确认能执行 `docker ps`。
5. **上传模板**：把 `docker-compose.yml` 传到应用目录，并从模板生成初始
   `.env`（自动写入 `DOMAIN` 和 `TRAEFIK_NETWORK`）和 `app.env`
   （自动生成随机 `API_SECRET_KEY`）。已有文件则跳过不覆盖。

执行方式（不带参数会进入全交互模式逐个询问）：

```bash
./deploy/vps-traefik/bootstrap.sh --server 1.2.3.4 --domain app.example.com
```

对于模板衍生的项目，`just copy <slug>` 会重写这些部署文件里默认的
`zata-ops` slug。你也可以手动覆盖：

```bash
./deploy/vps-traefik/bootstrap.sh \
  --app-slug my-app \
  --server 1.2.3.4 \
  --domain app.example.com
```

加 `-y` 跳过所有确认提示，适合脚本化执行。完整参数列表见
`./deploy/vps-traefik/bootstrap.sh --help`。

## 第三步：填写运行时配置

引导完成后，登录服务器编辑 `/opt/apps/<slug>/app.env`：

```bash
ssh -i ~/.ssh/cd-<slug> deploy@1.2.3.4
cd /opt/apps/<slug>
editor app.env
```

必须填写的项：

- `DATABASE_URL` — PostgreSQL 连接串。
- `ADMIN_PASSWORD_HASH` — 管理员密码的 bcrypt 哈希，生成方式：

  ```bash
  python -c 'import bcrypt; print(bcrypt.hashpw(b"你的密码", bcrypt.gensalt()).decode())'
  ```

- 各 AI provider 密钥（`OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 等，按实际用到的填）。
- 如果启用备份 profile，还需填 `S3_*` 备份存储配置。

`API_SECRET_KEY` 已由 bootstrap 自动生成，无需改动。
`.env` 里的 `BACKEND_IMAGE` / `FRONTEND_IMAGE` 占位镜像引用，
会在第一次 CI 部署时被自动改写为真实镜像。

## 第四步：首次启动应用

在服务器上以 `deploy` 用户执行（镜像需先推送到仓库）：

```bash
cd /opt/apps/<slug>
docker login <registry-host>   # 私有镜像需要
docker compose pull
docker compose up -d
```

如果镜像是私有的，CI 部署用的 `deploy` 用户也需要登录一次仓库，
登录凭证会保存在 `deploy` 用户的 `~/.docker/config.json`。

## 可选：GitHub Actions 自动部署

模板仓库默认的 `.github/workflows/cd.yml` 只构建发布归档，不做服务器部署。

要在衍生项目中启用这条可选的 VPS 部署路径：

```bash
cp deploy/vps-traefik/github-actions-deploy.yml.example \
  .github/workflows/deploy-vps-traefik.yml
```

配置仓库 secrets：

```text
REGISTRY_HOST
REGISTRY_NAMESPACE
REGISTRY_USERNAME
REGISTRY_PASSWORD
```

配置 `production` 环境 secrets（`SERVER_SSH_KEY` 填 bootstrap
生成的私钥文件 `~/.ssh/cd-<slug>` 的完整内容）：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
```

可选的 `production` 环境变量：

```text
PRODUCTION_DOMAIN
PRODUCTION_APP_DIR
```

该工作流会按 commit SHA 构建 backend、frontend 和 backup 镜像，
更新 `/opt/apps/<slug>/.env` 中的镜像引用，然后执行
`docker compose pull && docker compose up -d --remove-orphans`。

之后的每次发布只需要推送代码触发工作流，服务器上的 `app.env`
（密钥）不会被 CI 触碰。
