# 部署事件查看与回滚

`zata-ops deploy` 把 GitHub Actions 的部署事实暴露成三条命令，供人和 agent 使用：
查当前版本、列部署事件、按镜像 tag 回滚。它不维护本地部署状态库，GitHub 的
workflow run 与 Environment deployment 记录就是唯一事实源。

## 前提

- 本机安装并登录 `gh`：`gh auth status` 显示已登录且有仓库读取权限；回滚还需要
  `actions:write`（能触发 `workflow_dispatch`）。
- 目标仓库的 CD workflow 使用 `workflow_dispatch` 暴露回滚入口（本仓库 `cd.yml`
  的 `target_environment` + `rollback_tag` 输入即为此设计）。
- 私有 target 文件存在：`config/private/deploy_targets.toml`，字段格式参考仓库中的
  `config/deploy_targets.example.toml`。

```toml
[[targets]]
name = "prod"
repo = "your-org/your-repo"
workflow = "cd.yml"
environment = "production"
dispatch_ref = "main"
health_url = "https://<APP_DOMAIN>/health"
production = true
```

> 真实仓库名、域名和健康检查地址只写进 `config/private/`；本仓库是公开仓库。

## 命令

```bash
# 当前版本 + 最近 5 次部署 + 最新 workflow run
zata-ops deploy status --target prod

# 额外请求 health_url（可选）
zata-ops deploy status --target prod --check-health

# 部署事件列表
zata-ops deploy history --target prod --limit 10

# 回滚：先看计划，再加 --confirm 执行
zata-ops deploy rollback --target prod --to <sha>-production --dry-run
zata-ops deploy rollback --target prod --to <sha>-production --confirm
```

所有命令支持 `--json`，输出供 agent 解析的结构化结果。

## 回滚语义

`rollback` 触发的是 CD workflow 的 `workflow_dispatch`，传入 `rollback_tag`；
workflow 会跳过镜像构建，直接部署该 tag 对应的不可变镜像。因此：

- 回滚目标是镜像 tag（形如 `<sha>-<environment>`），不是分支或提交；
- 数据库迁移等有状态变更不会随回滚撤销，回滚前要判断该版本是否包含迁移；
- 不给 `--confirm` 时只打印计划并以退出码 3 结束，不会触发布署。

## 退出码与 JSON

| 退出码 | 含义 |
|---|---|
| 0 | 查询成功或已触发 |
| 1 | `gh` 调用失败（未登录、网络、权限、workflow 报错） |
| 2 | target 文件缺失、target 不存在或镜像 tag 非法 |
| 3 | 缺少 `--confirm`，仅输出回滚计划 |

`status` 的 JSON 主要字段：

```json
{
  "schema_version": 1,
  "operation": "deploy.status",
  "checked_at": "2026-01-01T12:00:00+08:00",
  "target": {"name": "prod", "repo": "owner/repo", "workflow": "cd.yml", "environment": "production", "production": true},
  "current_deployment": {"ref": "v0.5.36", "sha": "<sha>", "image_release": "<sha>-production", "status": "success", "created_at": "..."},
  "recent_deployments": ["..."],
  "latest_workflow_run": {"status": "completed", "conclusion": "success", "url": "https://github.com/..."},
  "health": {"ok": true, "status": 200, "latency_ms": 42}
}
```

## Agent 使用方式

`skills/agent-deploy/SKILL.md` 固定了三段流程：

1. **Plan**：`deploy status` / `deploy history --json` 确定当前版本与候选回滚目标，
   必要时用 `log-query` 技能查对应时间段的错误；
2. **Apply**：`deploy rollback --dry-run` 出计划，经人确认后 `--confirm`；
3. **Verify**：`--check-health` 加日志检索，比对部署前后的错误量、业务指标（翻译失败数、
   队列积压、账号池可用数），再给出保留或回滚建议。

证据（命令输出、run URL、前后指标）写入 `config/private/deploy-runs/`，与私有
清单一起保管，不要提交到仓库。

## 与监控栈的关系

集中日志仍由 `log-query` 技能经 Loki 查询；本命令不接入日志平台，也不替代
`container-observability-onboard`。部署事件的权威记录在 GitHub，Loki 用于验证
部署后的运行状态。
