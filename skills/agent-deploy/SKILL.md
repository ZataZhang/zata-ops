---
name: agent-deploy
description: 用 `zata-ops deploy` 查看 GitHub Actions 部署事件、判断当前线上版本并触发按镜像 tag 的回滚；负责 plan/apply/verify 三段流程与证据留档。不负责首次配置 SSH、构建流水线或日志平台接入。
---

# Agent 部署查看与回滚

用 `zata-ops deploy` 读 GitHub 的部署事实（workflow run + Environment deployment），
按 plan → apply → verify 执行，任何写操作都要先给人看计划。

## 前提核对

1. 仓库根目录执行 `just service-list` 之外的部署命令前，先确认 `gh auth status` 已登录；
   未登录时请用户在本机执行 `gh auth login`，不要代为输入凭据。
2. 读取 `config/private/deploy_targets.toml` 的 target 名称（不要打印文件内容中的域名与仓库细节，
   输出时按需遮盖）。文件缺失时提示用户按 `config/deploy_targets.example.toml` 创建。
3. 需要真实入口时优先用 `zata-ops deploy status --target <name> --check-health`，
   不要自行猜测健康检查 URL。

## Plan（只读）

```bash
zata-ops deploy status --target <name> --json
zata-ops deploy history --target <name> --limit 10 --json
```

- 从 `current_deployment` 读当前版本（ref / sha / image_release / status）；
- 从 `recent_deployments` 找「上一个成功且未包含有状态变更」的版本作为候选锚点；
- 需要解释失败原因时，用 `log-query` 技能按时间窗检索对应服务的错误，不要只看 run 结论；
- 输出计划：当前版本、目标版本、涉及的提交/PR、是否含数据库迁移、回滚影响面。

## Apply（写操作）

```bash
zata-ops deploy rollback --target <name> --to <sha>-<environment> --dry-run
zata-ops deploy rollback --target <name> --to <sha>-<environment> --confirm
```

- 必须先执行 `--dry-run` 并把 `command` 展示给用户；
- 只有用户明确确认后才执行 `--confirm`；退出码 3 表示未确认，不要重试；
- 生产 target 回滚前再次确认：目标 tag 存在、没有未执行的破坏性迁移、业务低峰窗口；
- 触发后把返回的 `workflow_run.url` 记录下来。

## Verify（部署后）

1. `zata-ops deploy status --target <name> --check-health --json` 确认新版本已生效、
   健康检查通过、deployment status 为 success；
2. 用 `log-query` 技能取部署前后相同长度的时间窗，比较错误量；
3. 业务指标（按项目实际）：翻译失败数、Celery 队列积压、Doc2X 账号池可用数；
4. 指标不达标时给出回滚建议与命令，不自行连续回滚。

## 证据与边界

- 证据写入 `config/private/deploy-runs/<timestamp>-<target>.md`（命令、JSON 摘要、run URL、
  前后指标、结论）；该目录属于私有配置，不要提交。
- 不直接 SSH 修改服务器配置、不手工 `docker service update`；只通过 workflow 回滚。
- 不把 token、域名、真实仓库地址写进对话或可提交文件。
- 部署事件以 GitHub 为准；本工具不做本地状态缓存，也不要手工维护部署记录表。
