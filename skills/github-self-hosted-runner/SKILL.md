---
name: github-self-hosted-runner
description: 在 Linux VPS 上安装、注册、迁移或排查 GitHub Actions self-hosted runner，并适配仓库工作流。适用于 runner 主机准备、Docker 权限选择、标签与实际 job 验证；应用镜像发布与远端部署由 github-vps-deploy 处理。
---

# GitHub 自托管 Runner

先读取目标仓库的 `AGENTS.md` 和相关规范，再查看现有 workflow、runner、主机服务与数据盘。区分三个结果：runner 已连接 GitHub、工作流能调度到指定 runner、实际 job 完成。前一个结果不能证明后一个。

## 决策

1. 确定注册范围（仓库或组织）、runner group 的仓库访问范围、主机架构和安装位置。已有 runner 时先核对身份与工作目录，不覆盖运行中的安装。
2. 搜索所有实际生效的 `.github/workflows/*.yml` 和 `*.yaml`，逐个记录 job 的 `runs-on`、`services`、Docker/Buildx/Compose、浏览器及系统依赖。嵌套目录中的 `.github/workflows` 只有在它本身是独立 GitHub 仓库时才生效。
3. 给目标 runner 设置项目或用途专属标签；迁移时用该标签限定 job，避免 `[self-hosted, Linux, X64]` 匹配组织内其他机器。组织 runner 还须核对 group 的仓库访问策略。
4. 按实际 job 选择 Docker 能力。无需 Docker 的 runner 不授予 Docker 权限；专用且仅运行受信任代码的主机可使用 `docker` 组；共享生产主机优先评估 Rootless Docker。读取 [Docker 与工作目录](references/docker-and-storage.md) 后再配置需要 Docker 的 runner。
5. PR 工作流会在 runner 上执行仓库代码。把接收不受信任 PR 的工作流放到隔离主机或临时 runner；不要因已有独立用户或 Rootless Docker 就认为生产主机已得到完整隔离。

## 安装与注册

`scripts/bootstrap.sh` 在目标 Linux 主机上运行，按 `prepare → register → service → status` 分阶段执行。它不负责修改 workflow 或配置 Docker。使用前查看脚本开头的变量说明，设置目标 URL、名称、专属标签、版本和 SHA-256。版本与 SHA-256 必须来自同一 GitHub runner release；不要复用文档中的旧校验值。注册 token 从调用脚本的标准输入传入，不写入仓库文件、日志或 shell 历史。GitHub 的 `config.sh` 要求 `--token` 参数，因此 token 会短暂出现在目标主机的进程参数中；注册时限制主机访问，结束后确认相关进程退出。脚本不会获取 GitHub 权限；已有 `gh` 管理权限时可通过对应的 repository/organization registration-token API 获取一次性 token，否则使用 GitHub 设置页面生成的 token。

`prepare` 只处理全新安装；遇到已存在的 runner 文件会保留现状。`register` 遇到 `.runner` 会停止并要求人工核对目标，而不是悄悄重注册。`service` 将 systemd 服务绑定到安装目录所在的挂载点；若使用 Rootless Docker，还须为服务设置 socket 环境和依赖，详见参考页。

## 工作流迁移与验证

- 在独立 worktree 修改目标仓库时，先辨认仓库自身规则和当前未提交改动。只调整用户要求的 job；若用户说“所有工作流”，仍需区分真正生效的 workflow 与嵌套示例。
- Docker service container 需要 runner 用户可访问的 Docker daemon。主机已有固定端口服务时，为 CI 数据库使用随机本机端口，并让测试连接 job 输出的实际端口；不要直接占用生产端口。
- 验证顺序：主机服务 `active`、GitHub 侧 runner `online` 且标签/访问范围正确、目标 job 显示指定 runner 名称、实际 service/构建/测试步骤通过。若无组织管理权限，明确记录 GitHub 侧访问范围未核验。
- runner 安装验证使用最小真实 job 或受影响的 workflow。项目全量测试属于仓库变更验证；遇到与 runner 无关的既有失败时记录证据，不为通过安装验收而反复搭建外部测试环境。
- 交付时记录安装位置、用户、标签、Docker 权限、工作目录、启动依赖、已验证到哪一层和回退办法。不要将注册 token、私钥或业务凭据写入交付内容。

`github-vps-deploy` 负责应用构建、镜像发布和 SSH 部署链路；本 skill 只负责承载这些 job 的 runner 主机及调度适配。
