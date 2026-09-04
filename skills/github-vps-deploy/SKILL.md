---
name: github-vps-deploy
description: 为已有项目创建或改进 GitHub Actions 持续部署流程，将容器镜像发布到镜像仓库并通过 SSH 部署到使用 Docker Compose 的自有 VPS，可适配宿主机 Traefik 入口；不用于集群编排、无容器部署或仅编写普通 CI 的请求。
---

# GitHub Actions 部署到 VPS

为当前项目生成一条与现有架构匹配、可验证且可回滚的部署链路。所有说明、注释和交付摘要使用中文；GitHub Actions 的键名、命令、变量名及产品专有名词保持英文。

## 开始前

1. 读取目标仓库的 `AGENTS.md` 及其要求的规范页。
2. 搜索并理解现有的 `.github/workflows/`、Dockerfile、Compose 文件、部署脚本、环境变量示例和运维文档；优先复用，避免另建平行部署体系。
3. 确认服务器使用普通 Docker Compose；如果项目实际采用集群编排，应说明本 Skill 不覆盖该部署形态，不要擅自改变服务器架构。
4. 盘点镜像构建矩阵：每个镜像的 context、Dockerfile、目标平台、运行服务和远端 env 键。
5. 若需要具体实现模式或 GitHub 配置清单，读取 [部署模式与实现要求](references/deployment-patterns.md)。
6. 如果用户尚未配置 GitHub Actions 到 VPS 的 SSH 访问，必须读取并交付 [首次配置 SSH 凭据](references/ssh-setup.md)，不能只列出 Secret 名称。

## 交付内容

按项目实际需要创建或更新：

- `.github/workflows/deploy.yml` 或仓库既有 CD workflow；
- 远端部署/预检脚本（复杂逻辑放脚本中，不在 YAML 内堆积大段 shell）；
- 非敏感配置示例，例如 `.env.example`；
- 部署文档，列清 GitHub Variables、Secrets、服务器一次性准备、触发方式、验证和回滚方法。
- SSH 首次配置说明，包括密钥生成、公钥安装、主机指纹核验、GitHub Secrets 填写和连接验证。

不要写入真实密钥、服务器地址或业务密码，不要自动配置 GitHub 仓库设置，也不要触发真实 workflow、push、发布或服务器部署，除非用户对这些外部变更另有明确授权。

## 工作流设计约束

- 将构建和部署拆成独立 job，部署 job 明确依赖构建成功。
- 镜像使用 `${{ github.sha }}` 等不可变 tag；允许额外推送可读 tag，但部署和回滚不能只依赖 `latest`。
- 设置最小 `permissions`。GHCR 通常只需 `contents: read` 与 `packages: write`。
- 使用 GitHub Environment 区分 staging/production，并使用 `concurrency` 防止同一环境并发发布。
- Secrets 只放凭据；主机名、端口、目录、域名等非敏感配置优先放 Variables。
- SSH 必须启用 `BatchMode=yes` 和 `StrictHostKeyChecking=yes`，使用预置 `known_hosts`，不要在 runner 内通过未经核验的 `ssh-keyscan` 建立信任。
- 远端稳定业务配置保留在服务器；workflow 只传本次发布所需的镜像引用和发布标识，避免覆盖服务器上的生产密钥。
- Traefik 已是宿主机公共入口时，应用栈只加入既有 external network 并声明 labels，不重复启动另一个 Traefik。
- 部署前验证必需输入、目标目录、Compose 渲染、Docker 网络和所需文件；失败时应在切流前退出。
- 部署后轮询真实公开健康检查 URL，并设定有限次数、间隔与明确超时；失败必须让 job 失败。
- 明确回滚入口，优先通过 `workflow_dispatch` 输入旧的 git SHA/镜像 tag，跳过重新构建并重新部署不可变镜像。
- 日志不得输出私钥、token、registry password 或完整业务 env；必要时只输出是否存在、长度或摘要。

## 实施原则

- 保持单机部署简单：构建并推送镜像，SSH 到固定应用目录，更新仅包含镜像引用的部署 env，然后执行 `docker compose pull` 与 `docker compose up -d --remove-orphans`。
- 多镜像可使用 matrix，但当构建参数、缓存或依赖差异较大时，可拆分 job 以保持清晰。
- 遵循仓库现有 action 版本固定策略；若仓库没有约定，使用官方 action 的受支持主版本，并在安全要求较高时固定完整 commit SHA。
- 不直接照抄参考项目中的项目名、域名、目录、镜像、服务数量或 Secrets 名称。

## 验证与交付

至少完成以下可在本地执行的验证：

1. 检查 workflow YAML 能被解析，且 GitHub 表达式未被本地模板误展开。
2. 对 shell 脚本运行 `bash -n`；若仓库提供 ShellCheck，则一并运行。
3. 使用示例 env 执行 `docker compose config`，确认 external network、镜像变量和 Traefik labels 可解析。
4. 检查 workflow 中引用的 Dockerfile、context、部署脚本和打包文件全部存在。
5. 对照文档核对每个 `${{ vars.* }}` 与 `${{ secrets.* }}` 都有配置说明。
6. 若无法连接真实服务器，在交付中明确说明未验证的边界以及首次发布前应执行的 dry-run/preflight。

最终说明创建或修改了哪些文件、用户需要配置哪些 Variables/Secrets、如何首次触发和如何回滚。不要把“YAML 可解析”等同于真实部署成功。
