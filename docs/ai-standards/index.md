# AI Standards Hub

`docs/ai-standards/` 是本仓库面向 AI 编码代理的**统一规范源目录**，也是 AI 相关规则的 `source of truth`。

目标很简单：

- 把通用规范按主题拆分，而不是按工具拆分
- 让 GitHub Copilot、Claude、Cursor、Codex 等入口共享同一套规则
- 避免 `AGENTS.md`、`CLAUDE.md`、`.github/copilot-instructions.md` 之类入口文件各自膨胀成独立规范正文

## Project Intent

本项目是主要供 AI Agent 使用的轻量运维工具箱。新增能力优先提供明确的 CLI 入口、配置约定与验证方式，
按命令需要支持执行预览、结构化结果和操作后检查。网页控制台、常驻管理服务和多用户权限暂不作为建设重点。

优先打通「私有配置 → 定位目标 → 预览操作 → 执行 → 验证结果」，再扩展命令数量。
同一套命令应允许人复现操作并检查结果；新增或调整命令时，应明确目标与影响范围、
适用的预览方式、成功判据和失败原因，并提供明确的退出码。
文档必须区分已实现能力与建设目标，不得把统一的预览、结构化输出或结果检查描述为所有命令已有的能力。

真实服务器资产、SSH 凭据引用和应用部署信息必须维护在被 Git 忽略的私有配置中；
仓库只提交工具实现、虚构配置示例、结构约束和使用文档。不得把真实清单写入文档、测试夹具或示例。
当前约定与验证入口见[私有运维配置](../guides/private-inventory.md)。
维护服务器清单时沿用公开示例的服务器 → SSH / 应用三级布局，新增或修改字段后运行
`just inventory-format config/private/servers.toml --write`，再运行 `just inventory-check`。
格式化会替换注释；长期信息保存到 `notes` 或私有快照，不把平铺追加当作完成维护。
Agent 修改现有私有配置前必须运行 `just private-backup`，成功后再修改。
出现手动复制提醒时告知用户；只有用户确认已完成复制后，才能运行 `just private-copy-done`，
不能以生成本机备份代替用户的手动复制确认。
凭据操作使用[服务与凭据管理](../guides/private-secrets.md)中的脚本：让用户本机隐藏输入，
查询仅列名称，使用时注入可信子进程。不得把凭据正文输出到对话、日志、报告、命令参数或工具调用文本。
服务清单保存凭据引用，密码及带凭据的 URL 存入私有 `secrets.env`；不得为了查看值而绕过脚本直接打印文件。
登记服务优先让用户执行 `just service-add`，以 `service-list` / `service-show` 查询非敏感元数据；
使用密码时按服务名称关联凭据，无需用户手工记住内部凭据名称。

## Source Of Truth

本目录是 AI 规范的主入口，但不是唯一的详细技术文档来源。

权威关系如下：

- `docs/ai-standards/`：AI 通用规范主入口
- `docs/architecture/system-design.md`：后端四层架构的详细权威文档
- `tests/playwright-e2e/README.md`：Playwright 包的详细适配说明

工具入口文件只做适配：

- `AGENTS.md`
- `CLAUDE.md`
- `.cursor/commands/cursor.md`
- `.github/copilot-instructions.md`
- `.github/instructions/*.instructions.md`

## Read Order

开始任务时，建议按这个顺序读取：

1. 本页
2. 与任务最相关的标准页
3. 若涉及后端新功能，再读 `docs/architecture/system-design.md`
4. 若涉及 Playwright，再读 `tests/playwright-e2e/README.md`

## Standards Map

- [Architecture](architecture.md)
- [Code Reuse](code-reuse.md)
- [Naming](naming.md)
- [Comments And Docstrings](comments-docstrings.md)
- [Documentation](documentation.md)
- [Testing](testing.md)
- [Tooling](tooling.md)

## When To Update This Hub

以下情况应同步更新本目录：

- 架构边界或依赖方向发生变化
- 命名、Docstring、编码或注释规范发生变化
- 常用命令、工具链或验证流程发生变化
- 新增一类长期维护的技术子树，例如新的前端约束或新的测试栈

## Maintenance Rules

- 优先修改本目录中的主题页，再同步各工具入口
- 入口文件应保持简短，只保留最关键的高信号摘要
- 不要把一整份长文复制到多个入口文件中
- 若新增主题页，同时更新 `mkdocs.yml` 导航和相关入口文件引用
