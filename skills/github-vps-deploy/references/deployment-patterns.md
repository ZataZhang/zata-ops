# 部署模式与实现要求

仅在需要设计或实现 workflow 时读取本页。

## Docker Compose + 宿主机 Traefik

适合单机 VPS。典型链路：

```text
push tag / workflow_dispatch
  -> 构建并推送 SHA 镜像
  -> SSH 到固定 APP_DIR
  -> 更新部署 env 中的 IMAGE 变量
  -> docker compose pull
  -> docker compose up -d --remove-orphans
  -> 公开 URL 健康检查
```

Compose 文件应把服务加入宿主机已有的 external Traefik 网络，并通过 labels 声明 router、entrypoint、TLS resolver 与容器端口。数据库等状态服务通常不应加入公共入口网络。

如果目标服务器尚未安装 Docker/Traefik，可在文档中提供一次性初始化步骤；若当前环境提供 `zata-ops`，可建议先执行：

```bash
zata-ops env provision --host <host> --user <user> --acme-email <email> --dry-run
zata-ops env provision --host <host> --user <user> --acme-email <email>
```

不要把服务器初始化混入每次应用发布。

## 推荐触发规则

- `push` 到 `main`：可用于 staging，但只有项目明确采用该约定时启用。
- `push` 匹配 `v*` 的 tag：适合 production 发布。
- `workflow_dispatch`：提供目标环境和可选 `rollback_tag`。
- production job 使用 GitHub `environment: production`，以便配置保护规则和人工审批。

回滚输入必须是已存在的不可变镜像 tag。存在 `rollback_tag` 时跳过 build job，只执行部署与健康检查。

## GitHub 配置清单

名称应根据仓库已有约定调整。推荐分类如下：

| 类型 | 推荐字段 | 说明 |
| --- | --- | --- |
| Variable | `REGISTRY_HOST` | 如 `ghcr.io` |
| Variable | `IMAGE_NAMESPACE` | 镜像命名空间 |
| Variable | `SERVER_HOST` | VPS 主机名；若组织认为敏感也可放 Secret |
| Variable | `SERVER_PORT` | SSH 端口，默认 22 |
| Variable | `SERVER_USER` | 最小权限部署用户 |
| Variable | `APP_DIR` / `RELEASE_DIR` | 远端部署目录 |
| Variable | `HEALTHCHECK_URL` | 真实公网健康检查入口 |
| Secret | `SSH_PRIVATE_KEY` | 专用部署私钥 |
| Secret | `SSH_KNOWN_HOSTS` | 线下核验后的 known_hosts 内容 |
| Secret | `REGISTRY_USERNAME` | 非 GHCR 或远端拉取需要时 |
| Secret | `REGISTRY_PASSWORD` | registry token/password |

使用 GHCR 时，runner 推送通常可用 `${{ github.actor }}` 和 `${{ github.token }}`；服务器拉取私有镜像仍需具备 `read:packages` 的凭据。不要假定 runner 的临时 token 可长期保存在服务器。

## 健康检查与发布证据

- 容器启动不代表应用可用；应检查公网 HTTPS URL。
- 健康检查循环必须有最大尝试次数和单次请求超时。
- 根据项目补充关键页面/API，但不要把仅需 2xx 的端点硬编码为必须返回 200，除非契约明确。
- 输出 release ID、镜像引用、目标环境和最终服务状态，避免输出敏感 env。
- 可把摘要写入 `$GITHUB_STEP_SUMMARY`，方便审核发布证据。

## 来源经验

本模式吸收了两个本地实现的可复用经验：

- `zata-ops/deploy/vps-traefik/github-actions-deploy.yml.example`：单机 Compose、外部 Traefik 网络和 SSH 更新镜像。
- `TransMaster/.github/workflows/cd.yml`：环境分流、不可变镜像、严格 known_hosts、健康检查和 rollback tag。

这些文件只用于理解设计，不是可直接复制的模板。生成结果必须以当前目标仓库为准。
