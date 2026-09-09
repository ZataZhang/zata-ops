# 在 VPS 上安装 GitHub Actions Self-hosted Runner

本文记录 2026-09-09 为 `ZataZhang/freshai` 安装 runner 的过程，也可作为其他私有仓库的操作模板。
这是手工运维流程，目前不是 `zata-ops` CLI 子命令。GitHub 负责触发与调度，VPS 执行构建和测试。

> 隐私说明：本文使用文档示例 IP `192.0.2.10`，执行命令前请替换为自己的服务器地址。

## 已部署实例

| 配置 | 实际值 |
| --- | --- |
| SSH 地址 | `root@192.0.2.10`（本地 SSH 密钥认证） |
| 系统 | Ubuntu 24.04.4 LTS / x86_64 |
| 资源 | 2 核、约 8 GB 内存、80 GB 系统盘 |
| 仓库 | `ZataZhang/freshai`，安装时为私有仓库 |
| Runner | `freshai-runner-01` |
| 自定义标签 | `freshai` |
| 用户 / 目录 | `github-runner` / `/opt/actions-runner` |
| 初始版本 | `2.337.0`，保留默认自动更新 |
| 服务 | `actions.runner.ZataZhang-freshai.freshai-runner-01.service` |

服务器已有 Docker、Buildx、Compose，以及监控容器和 Traefik。本次安装一个 runner 实例，
同一时刻执行一个 job，其他任务排队。不同仓库复用此流程时，应替换仓库地址、名称和标签。

## 1. 检查主机与仓库

在本地执行：

```bash
ssh -o BatchMode=yes root@192.0.2.10
```

在服务器检查架构、容量和既有服务：

```bash
uname -m
cat /etc/os-release
nproc
free -h
df -h /
docker ps --format '{{.Names}} {{.Status}}'
docker compose version
docker buildx version
```

下面的安装包仅适用于 Linux x64；ARM64 机器需从 GitHub 页面选择对应架构。
需要访问 GitHub 及工作流依赖的下载站点；runner 主动连接 GitHub，无需为接收任务新增公网入站端口。

在 GitHub 仓库 **Settings → Actions → Runners → New self-hosted runner** 选择 Linux / x64。
需要仓库管理权限。已有本地 `gh` 登录时，也可以检查：

```bash
gh repo view ZataZhang/freshai --json visibility
gh api repos/ZataZhang/freshai/actions/runners \
  --jq '.runners[] | {name,status,labels:[.labels[].name]}'
```

常驻 runner 只执行受信任代码。公开仓库或不受信任 PR 需要独立隔离方案，不能直接套用此共享主机配置。
`docker` 组权限等同主机管理权限，专用用户不构成 Docker 安全隔离。

## 2. 创建用户并安装

以下命令在服务器的 root shell 中执行。已有同名安装时先检查服务和目录，不要覆盖正在工作的 runner。

```bash
id github-runner >/dev/null 2>&1 || useradd --create-home --shell /bin/bash github-runner
usermod -aG docker github-runner
install -d -o github-runner -g github-runner /opt/actions-runner
cd /opt/actions-runner
```

以下是本次实际使用的版本及 SHA256。以后安装应从 GitHub 页面取得当时的版本、URL 和对应摘要，三者一起更新：

```bash
curl --fail --location --retry 3 \
  -o actions-runner-linux-x64-2.337.0.tar.gz \
  https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz
echo '70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613  actions-runner-linux-x64-2.337.0.tar.gz' \
  | sha256sum -c -
```

确认输出 `OK` 后继续：

```bash
sudo -u github-runner tar xzf actions-runner-linux-x64-2.337.0.tar.gz
./bin/installdependencies.sh
```

依赖脚本会通过系统包管理器安装或升级依赖；Ubuntu 的 `needrestart` 可能重启系统服务。
在共享服务器上应安排合适的维护时间。本次脚本尝试多个 ICU 包名后使用系统已有的 `libicu74`，
中间某个候选包不存在不等于最终安装失败，应检查脚本退出状态和 runner 启动结果。

## 3. 注册仓库

使用 GitHub 页面生成的注册 token，它约一小时过期，不是长期 PAT。
在服务器 root 的 Bash shell 中输入 token，不把实际值写进命令历史或文档：

```bash
cd /opt/actions-runner
read -r -s -p 'Runner registration token: ' runner_token
printf '\n'
sudo -u github-runner ./config.sh --unattended \
  --url https://github.com/ZataZhang/freshai \
  --token "$runner_token" \
  --name freshai-runner-01 \
  --labels freshai \
  --work _work
unset runner_token
```

本次实际使用本地 `gh` 获取新 token，经 SSH 标准输入传入，避免在服务器保存 GitHub 登录凭据。
也可在本地 Bash / Zsh 中采用此替代方式（与上面二选一）：

```bash
set -o pipefail
gh api --method POST repos/ZataZhang/freshai/actions/runners/registration-token --jq .token \
  | ssh -o BatchMode=yes root@192.0.2.10 \
    'IFS= read -r runner_token; cd /opt/actions-runner && sudo -u github-runner ./config.sh --unattended --url https://github.com/ZataZhang/freshai --token "$runner_token" --name freshai-runner-01 --labels freshai --work _work'
```

看到 `Runner successfully added` 和 `Settings Saved` 后再安装服务。

## 4. 后台运行和开机启动

在服务器 root shell 中执行：

```bash
cd /opt/actions-runner
./svc.sh install github-runner
./svc.sh start
./svc.sh status
```

`./run.sh` 只适合前台调试；长期运行使用上述 systemd 服务，不需要保持 SSH 会话。
Ubuntu 如启用了 `needrestart`，按官方说明排除 runner 服务，避免它在 job 中途自动重启：

```bash
printf '%s\n' '$nrconf{override_rc}{qr(^actions\.runner\..+\.service$)} = 0;' \
  > /etc/needrestart/conf.d/actions_runner_services.conf
```

仅在该配置目录存在时使用此命令。此设置不代替系统安全更新，维护时仍需主动安排服务重启。

## 5. 修改工作流

每个需要迁移的 job 使用：

```yaml
runs-on: [self-hosted, linux, x64, freshai]
```

FreshAI 修改了 `.github/workflows/ci.yml`、`cd.yml` 和 `deploy.yml` 中的全部 job。
自定义标签可避免任务被另一个 Windows runner `WARGOD-PC` 接走。
Python、Node.js、uv、pnpm 继续由现有 setup actions 安装，Docker / Compose / Buildx 由主机提供。

提交并推送工作流后才会生效。重跑旧提交的任务仍使用旧工作流，应在新提交上触发 CI。
生产部署可能由 tag 或手工操作触发，不要把生产发布当作安装 smoke test。

## 6. 验证

在服务器执行：

```bash
systemctl is-enabled actions.runner.ZataZhang-freshai.freshai-runner-01.service
systemctl is-active actions.runner.ZataZhang-freshai.freshai-runner-01.service
journalctl -u actions.runner.ZataZhang-freshai.freshai-runner-01.service -n 30 --no-pager
sudo -u github-runner -H docker info --format '{{.ServerVersion}}'
sudo -u github-runner -H docker compose version
sudo -u github-runner -H docker buildx version
```

预期服务为 `enabled` / `active`，日志包含 `Connected to GitHub` 和 `Listening for Jobs`。
在本地用新的 API 请求确认 GitHub 侧状态：

```bash
gh api repos/ZataZhang/freshai/actions/runners \
  --jq '.runners[] | {name,status,busy,labels:[.labels[].name]}'
gh run list --repo ZataZhang/freshai --workflow ci.yml --limit 3
```

进一步在 Actions 的 job 页面确认 runner 名称与全部步骤结果。
本次安装已验证：服务自启、GitHub Online、Docker 权限、原有容器运行正常；
工作流提交 `a3fcd2ba` 推送后，
[CI 运行 34301617169](https://github.com/ZataZhang/freshai/actions/runs/34301617169)
的 Frontend Build 已由 `freshai-runner-01` 接收，Validate Template 当时排队。
这是实际任务接收证据，记录时未确认完整 CI 最终结果，不代表全部测试通过。

## 日常管理与排障

```bash
cd /opt/actions-runner
sudo ./svc.sh status
sudo ./svc.sh stop
sudo ./svc.sh start
```

- **任务排队**：检查 runner 在线状态、所有标签是否匹配，以及当前是否有 job 占用；一个实例同时只跑一个 job。
- **Docker permission denied**：检查 `id github-runner`；修改组权限后重启 runner 服务使新进程生效。
- **注册失败**：检查 token 是否过期、仓库 URL 和管理权限；重新获取 token。
- **运行失败**：查看 Actions job 日志及 `/opt/actions-runner/_diag/`，确认系统依赖和下载网络可用。
- **磁盘增长**：检查 `df -h`、`docker system df`、`_work` 和 `_diag`；常驻主机不会在每个 job 后销毁。
  共享主机禁止无差别清理 Docker 资源，避免影响监控和 Traefik。
- **额度问题**：self-hosted 执行与 GitHub artifact / 缓存服务是不同资源，迁移 runner 不会消除存储额度问题；计费以官方实时说明为准。
- **下线**：先停止并卸载服务（`sudo ./svc.sh uninstall`），再按 GitHub Runners 页的 Remove 流程注销。
  删除工作目录前确认无需保留日志和构建产物。

## 官方参考

- [添加 self-hosted runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [配置系统服务](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application)
- [Runner 网络与运行要求](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
