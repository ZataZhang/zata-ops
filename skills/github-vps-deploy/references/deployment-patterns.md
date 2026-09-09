# 部署模式与实现要求

在设计、实现 workflow 或排查发布失败时读取本页。

## Docker Compose + 宿主机 Traefik

适合单机 VPS。典型链路：

```text
push tag / workflow_dispatch
  -> 构建并推送 SHA 镜像
  -> SSH 到固定 APP_DIR
  -> 生成候选部署 env 并校验
  -> 用候选 env 预拉取镜像（有限重试）
  -> 备份并切换部署 env
  -> docker compose up -d --pull never --remove-orphans
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

首次配置需要同时写入 GitHub 与 VPS 时，优先通过项目初始化脚本消费一个 Git 忽略的 `.env`，不要让用户在网页、终端和服务器之间手工复制每个字段。初始化 `.env` 可包含 registry 凭据，但上传 VPS 时必须生成字段白名单明确的部署 env，不能原样复制。

推荐把初始化和日常发布分开：

```text
一次性 setup script
  -> 创建项目专用部署用户与目录
  -> 生成/复用 SSH 密钥并核验 host key
  -> gh 配置 Environment、Secrets、Variables
  -> docker login + 上传 Compose/稳定 env

每次 tag workflow
  -> 构建/推送不可变镜像
  -> 严格 SSH 更新镜像引用
  -> Compose 部署 + 公网健康检查
```

setup script 应提供只读预检，且不能顺便创建 tag、push 或启动生产服务。日常 workflow 不应重复创建用户、轮换密钥、覆盖业务 env 或重新初始化 Traefik。

## 健康检查与发布证据

- 容器启动不代表应用可用；应检查公网 HTTPS URL。
- 健康检查循环必须有最大尝试次数和单次请求超时。
- 根据项目补充关键页面/API，但不要把仅需 2xx 的端点硬编码为必须返回 200，除非契约明确。
- 输出 release ID、镜像引用、目标环境和最终服务状态，避免输出敏感 env。
- 可把摘要写入 `$GITHUB_STEP_SUMMARY`，方便审核发布证据。

## 镜像推送兼容性与拉取重试

- 按日志区分构建、推送、VPS 拉取、容器创建、应用就绪和公网入口，不把后续阶段的失败归为“编译失败”。
- 若构建成功，但在导出 attestation 后推送报 `unknown manifest class for application/vnd.oci.empty.v1+json`，应排查镜像仓库对证明元数据格式的支持。已验证的兼容方案是在 `docker/build-push-action` 设置 `provenance: false`、`sbom: false`；这会失去对应供应链元数据，不能无条件用于所有仓库。需要保留证明时，应核验目标仓库和构建工具支持的格式。
- `TLS handshake timeout`、认证端点的 `connection reset by peer` 是连接失败证据，不等于密码错误，也不能仅凭日志断定是哪一端重置连接。不要因此关闭 TLS 校验或打印凭据。
- 镜像预拉取可采用最多 3 次、间隔 5 秒等有限重试；耗尽则保留当前配置并退出非零。鉴权拒绝、镜像不存在等确定性失败应定位配置，不靠无限重跑解决。
- 显式 `pull` 成功后，`up` 仍可能因 `pull_policy: always` 再次访问仓库。发布脚本通过 `up --pull never` 使用预拉取的不可变镜像；缺失镜像应明确失败，而不是悄悄改用其他版本。

## 切换镜像仓库

更换 registry 时有三处状态要按顺序改齐，漏掉任何一处都会让发布卡在中途：

1. GitHub Environment：更新 `REGISTRY_HOST`（Variable）与 `REGISTRY_USERNAME`、`REGISTRY_PASSWORD`（Secret）。同时在 registry 控制台确认目标命名空间已存在——命名空间缺失会让构建成功后推送被拒，重跑虽便宜但多等一轮。
2. 服务器拉取凭据：触发发布前，部署用户必须先登录新仓库，否则 `docker compose pull` 直接失败。本地没有部署私钥（CI-only 或已丢失）时，通过可信管理员通道以部署用户身份执行：

   ```bash
   printf '%s' "$REGISTRY_PASSWORD" | ssh -o BatchMode=yes <admin>@<host> \
     "sudo -H -u <deploy-user> docker login <new-registry-host> --username <user> --password-stdin"
   ```

3. 触发发布：workflow 以新 `REGISTRY_HOST` 更新服务器 `.env` 中的镜像引用并预拉取；构建通常命中缓存，验证重点在推送与服务器拉取两个阶段。

验证与收尾：

- 不要用 `docker system info | grep Username` 判断登录状态，Docker 29 起不再输出该字段；直接以部署用户 `docker pull` 一个真实镜像，或看发布 run 的拉取阶段是否通过。
- 完成切换后以部署用户对旧仓库执行 `docker logout <old-registry-host>`，并为迁移过程中暴露过的密码安排轮换。

## 首次发布与回滚边界

- 初始化占位 tag 只用于配置渲染，不代表存在可运行的旧版本。切换前记录是否有真实旧发布；首次发布失败时恢复配置并报告可能残留的容器，不去拉取占位镜像，也不自动删除数据卷。
- 有旧发布时优先使用保留在本地的旧镜像回滚，并禁止启动阶段再次拉取。回滚所需镜像必须纳入保留策略；仅有格式正确的 SHA 不证明镜像存在或旧版本部署成功。
- 保留原始失败退出码，单独报告回滚失败，避免错误处理递归；区分“配置已恢复”和“服务已恢复”。公网检查失败是否自动回滚也要明确，不能仅有手动回滚入口就宣称全流程自动恢复。
- 用真实发布脚本、临时应用目录和替代 Docker 命令覆盖：正常发布、暂时失败后成功、重试耗尽不切换、首次启动失败、旧版本恢复及恢复失败。断言退出码、调用顺序、配置恢复和临时文件清理；这些是脚本集成验证，不是生产上线证据。

## 服务器资源与 Compose 同步

- 发布前读取实际 CPU 数、可用内存、swap、已有容器占用及磁盘空间。CPU 限额超过宿主机可用 CPU 会导致容器创建失败；内存限额和整体预算也应为系统、Traefik 及其他服务留余量。
- 按实际服务数量、启动峰值和运行负载分配资源，不套用固定的前后端配额。发布后检查 OOM、重启计数和真实内存使用，再评估负载容量；空闲状态启动成功不代表容量满足生产负载。
- 确认远端 Compose 是否与预期版本一致，可比较校验和。若发布流程不上传 Compose，本地修改、提交和打 tag 都不会自动改变远端资源限制。
- 同步时先检查远端差异并备份，上传候选文件后用目标环境执行 `docker compose config --quiet`，保留部署用户所有权，再原子替换；不覆盖业务 `.env`、`app.env` 或密钥。若决定让 CD 管理 Compose，要同时设计版本化和回滚，而非只上传一个无法恢复的文件。

## 冷启动与监控终点

- 健康检查应覆盖迁移和应用初始化的冷启动耗时，结合目标机器资源设置 `start_period`、`interval`、`timeout` 和 `retries`。不要通过删除检查、返回假成功或无限等待规避启动失败。
- 若 Compose 因依赖 `unhealthy` 失败，稍后后端却变为 `healthy`，检查启动时间线、健康检查记录、OOM 与重启次数，判断是否等待预算不足。前端可能仍处于 `Created`，后端恢复不会自动证明整套应用已启动。
- 服务随后就绪并在重试后通过公网检查，只证明暖启动重试成功，不证明下次冷启动已修复。需明确披露并调整、验证冷启动预算。
- 用户授权发布并监控时，跟踪当前 commit/tag 对应的 run 至终态。暂时性失败可有限重试，并区分“直接重跑原 run 使用原 workflow”与“发布新提交使用新 workflow”。遇到确定性错误则报告证据，不擅自扩大到改密码、删除数据或调整其他服务。
- 最终报告 commit、tag、run 链接、容器状态和实际 HTTPS 结果；Actions 成功、容器 healthy 和公网可达是不同证据，不等于全部业务流程均已验证。
