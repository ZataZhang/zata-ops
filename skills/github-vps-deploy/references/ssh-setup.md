# 首次配置 SSH 凭据

当 GitHub Actions 尚不能登录目标 VPS 时，必须向用户交付完整步骤。命令中的值应替换为当前项目的真实设置；不要把尖括号占位符直接执行。

## 默认交付：初始化脚本

当创建用户、生成密钥、核验指纹、写 GitHub Environment 和 registry 登录需要多条命令时，优先在目标仓库生成一个项目初始化脚本，并把下文手工流程保留为排障文档。用户只需复制 `.env.example`、填写 `.env`、先运行只读预检，再正式执行脚本。

推荐配置流：

```text
<deploy-dir>/.env.example  -> 提交；只含安全默认值和空凭据
<deploy-dir>/.env          -> Git 忽略、chmod 600；用户填写
<deploy-dir>/.credentials/ -> Git 忽略、chmod 700；保存项目专用密钥
```

脚本至少应具备以下行为：

1. 无真实 `.env` 时，从 `.env.example` 创建并停止，让用户先填写。
2. `--check` 或 `--dry-run` 只做配置、依赖、GitHub 权限、可信 root/admin SSH、Docker 和网络检查，不创建用户、密钥、GitHub 设置或远端文件。
3. 正式模式生成或复用单仓库 Ed25519 密钥；缺一半的密钥对必须停止，不得静默覆盖。
4. 通过已有可信管理员 SSH 幂等创建项目专用用户、`authorized_keys` 和应用目录。默认用户名从项目 slug 推导为 `<slug>-deploy`，并允许配置覆盖。
5. 从可信 SSH 会话读取 `/etc/ssh/ssh_host_ed25519_key.pub`，同时单独运行 `ssh-keyscan`，比较两者指纹后生成 `SSH_KNOWN_HOSTS`。
6. 使用严格主机校验和新私钥验证部署用户的 Docker 与目录权限。
7. 使用 `gh api`、`gh secret set`、`gh variable set` 创建或更新 Environment 配置；所有密码和私钥都从标准输入传递。
8. 使用 `docker login --password-stdin` 配置服务器拉取权限，并只上传不含初始化凭据的部署元数据。
9. 输出密钥保存路径和下一步，但不输出密钥、token、完整业务 env，也不自动发布。

`.env.example` 的字段按项目裁剪，常见内容如下：

| 类型 | 字段示例 |
| --- | --- |
| GitHub | `GITHUB_REPOSITORY`、`GITHUB_ENVIRONMENT` |
| VPS | `SERVER_HOST`、`SERVER_PORT`、`SERVER_ROOT_USER`、`SERVER_USER`、`APP_DIR` |
| Registry | `REGISTRY_HOST`、`IMAGE_NAMESPACE`、`REGISTRY_USERNAME`、空的 `REGISTRY_PASSWORD` |
| Routing | `DOMAIN`、`HEALTHCHECK_URL`、`TRAEFIK_NETWORK` |

不要把本地初始化 `.env` 原样上传到 VPS，因为它可能包含 registry password。应生成一个经过字段白名单筛选的远端部署 env。应用的数据库 DSN、API keys 和认证密钥继续放在独立的服务器运行 env 中。

下文是无法使用自动脚本时的手工排障流程。

## 应用运行配置的首次上传与更新

区分本地初始化配置（包含 SSH/GitHub/registry 设置）和应用配置（数据库、认证、模型密钥）。用户明确指定项目根目录 `.env` 或其他文件作为应用配置来源时，让初始化脚本直接上传，不再要求用户到服务器逐项重填。

- 文档明确源文件和远端目标，例如项目根目录 `.env` → `$APP_DIR/app.env`；不将初始化 `.env` 当作应用配置。
- 检查源文件存在、可读且非空。按原始字节传输，不用 `source`/`eval` 执行应用配置，不在日志中打印内容；不隐式合并 `.env.local` 或替换数据库地址。
- 首次目标不存在时上传；已有目标默认保留。提供 `--update-app-env` 或等价显式入口，并说明它执行完整初始化还是仅同步配置。
- 更新时在目标目录创建权限 `600` 的临时文件，传输完整且校验通过后才原子替换；失败应保留旧目标。替换前保存权限 `600` 的上一份备份，并说明保留策略。
- `--check` 即使与更新参数同时使用，也不得上传或覆盖远端配置。
- 配置上传不等于容器已加载新配置；明确是否需要重建/重启，不因上传请求自行扩展为生产发布。
- 提醒核对本地地址、路径和开发数据库是否适用于生产；是否共享数据库和密钥由用户的部署选择决定。

使用临时目录与虚构配置验证首次写入、默认保留、显式替换、备份、权限、特殊字符原样保留和失败不破坏旧文件。真实上传未经验证时披露这一边界，不把仓库业务测试通过当作传输验证。

## 凭据流向

推荐在可信本机生成一对仅供该仓库部署使用的 Ed25519 密钥：

```text
可信本机生成密钥对
  ├── 公钥  -> VPS 部署用户的 ~/.ssh/authorized_keys
  └── 私钥  -> GitHub Environment Secret: SSH_PRIVATE_KEY

VPS SSH 主机公钥（核验指纹后）
  └── known_hosts -> GitHub Environment Secret: SSH_KNOWN_HOSTS
```

不要把私钥提交进仓库。不要优先在 VPS 上生成再导出私钥；这样会增加私钥留存在服务器、终端历史或传输过程中的风险。

## 1. 在可信本机生成专用部署密钥

在仓库目录之外的临时目录执行：

```bash
DEPLOY_KEY_DIR="$(mktemp -d)"
ssh-keygen \
  -t ed25519 \
  -C "github-actions:<owner>/<repo>" \
  -f "$DEPLOY_KEY_DIR/id_ed25519" \
  -N ""
chmod 600 "$DEPLOY_KEY_DIR/id_ed25519"
```

GitHub Actions 无法交互输入 passphrase，因此这里使用无口令的、单仓库专用密钥。不要复用个人 SSH 私钥或其他服务器的管理密钥。

执行者必须告诉用户 `$DEPLOY_KEY_DIR` 的实际路径，但不得在聊天或日志中输出私钥内容。

## 2. 在 VPS 准备部署用户

优先使用已经存在的非 root 部署用户。若需要创建，可由有 sudo 权限的管理员在 VPS 上执行：

```bash
sudo useradd --create-home --shell /bin/bash deploy
sudo usermod -aG docker deploy
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
sudo touch /home/deploy/.ssh/authorized_keys
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

加入 `docker` 组通常等同于授予主机 root 级能力。必须让用户知道这一点；如组织有更严格策略，应使用受限 sudo、rootless Docker 或专用部署服务替代。

部署目录应归该用户管理，例如：

```bash
sudo install -d -m 750 -o deploy -g deploy /opt/apps/<app-name>
```

## 3. 把公钥安装到 VPS

回到生成密钥的可信本机，设置连接参数并安装公钥：

```bash
DEPLOY_HOST="<server-host-or-ip>"
DEPLOY_PORT="22"
DEPLOY_USER="<project-slug>-deploy"
DEPLOY_APP_DIR="/opt/apps/<app-name>"

ssh-copy-id \
  -i "$DEPLOY_KEY_DIR/id_ed25519.pub" \
  -p "$DEPLOY_PORT" \
  "$DEPLOY_USER@$DEPLOY_HOST"
```

然后验证专用私钥可以无交互登录，并确认 Docker 与目标目录权限：

```bash
ssh \
  -i "$DEPLOY_KEY_DIR/id_ed25519" \
  -p "$DEPLOY_PORT" \
  -o BatchMode=yes \
  "$DEPLOY_USER@$DEPLOY_HOST" \
  "docker version >/dev/null && test -w '$DEPLOY_APP_DIR' && echo deploy-access-ok"
```

验证失败时先修复权限，不要通过关闭 SSH 主机校验或改用 root 掩盖问题。

## 4. 获取并核验 VPS 主机指纹

在 VPS 控制台或一个已经可信登录的会话中查看服务器 Ed25519 主机密钥指纹：

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

在可信本机扫描主机公钥并查看指纹：

```bash
ssh-keyscan \
  -p "$DEPLOY_PORT" \
  -t ed25519 \
  "$DEPLOY_HOST" > "$DEPLOY_KEY_DIR/known_hosts"

ssh-keygen -lf "$DEPLOY_KEY_DIR/known_hosts"
```

必须人工确认两边显示的 `SHA256:` 指纹完全一致。`ssh-keyscan` 只负责获取公钥，本身不能证明公钥属于目标服务器。指纹不一致时立即停止，不要把扫描结果写入 GitHub。

核验完成后测试严格主机校验：

```bash
ssh \
  -i "$DEPLOY_KEY_DIR/id_ed25519" \
  -p "$DEPLOY_PORT" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$DEPLOY_KEY_DIR/known_hosts" \
  "$DEPLOY_USER@$DEPLOY_HOST" \
  'echo strict-ssh-ok'
```

## 5. 填入 GitHub Environment

推荐创建 `production` Environment，并在该 Environment 下填写配置。网页路径是：

```text
GitHub repository
  -> Settings
  -> Environments
  -> production
  -> Environment secrets / Environment variables
```

Secrets：

- `SSH_PRIVATE_KEY`：`$DEPLOY_KEY_DIR/id_ed25519` 的完整内容，包括头尾标记。
- `SSH_KNOWN_HOSTS`：已核验的 `$DEPLOY_KEY_DIR/known_hosts` 完整内容。

Variables：

- `SERVER_HOST`：`$DEPLOY_HOST`。
- `SERVER_PORT`：`$DEPLOY_PORT`。
- `SERVER_USER`：`$DEPLOY_USER`。
- `APP_DIR`：`$DEPLOY_APP_DIR`，例如 `/opt/apps/my-app`。

如果已安装并登录 GitHub CLI，可在仓库目录执行：

```bash
gh secret set SSH_PRIVATE_KEY --env production < "$DEPLOY_KEY_DIR/id_ed25519"
gh secret set SSH_KNOWN_HOSTS --env production < "$DEPLOY_KEY_DIR/known_hosts"
gh variable set SERVER_HOST --env production --body "$DEPLOY_HOST"
gh variable set SERVER_PORT --env production --body "$DEPLOY_PORT"
gh variable set SERVER_USER --env production --body "$DEPLOY_USER"
gh variable set APP_DIR --env production --body "$DEPLOY_APP_DIR"
```

通过 `gh` 写入 GitHub、通过 SSH 修改 VPS，以及触发 workflow 都是外部状态变更。代理只有得到用户明确授权后才能代为执行；否则只生成命令和说明。

## 6. 清理与首次验证

确认 GitHub Secrets 保存成功后，用户应把临时私钥移入受保护的密码管理/密钥存储，或安全删除临时目录。代理不得未经明确授权删除密钥文件。

首次运行 workflow 前，检查 workflow 使用：

```yaml
environment: production
```

并确认 SSH 命令包含：

```text
BatchMode=yes
StrictHostKeyChecking=yes
```

首次 workflow 应先只验证 SSH、Docker 和 Compose 配置，再执行正式发布。验证完成后，检查公开健康地址以及服务器上的：

```bash
docker compose ps
docker compose logs --tail 100
```

## 吊销与轮换

需要吊销时，从 VPS 部署用户的 `authorized_keys` 删除对应公钥，并删除或更新 GitHub 中的 `SSH_PRIVATE_KEY`。轮换时生成全新密钥对，先并行加入新公钥并验证 workflow，再移除旧公钥，避免发布中断。
