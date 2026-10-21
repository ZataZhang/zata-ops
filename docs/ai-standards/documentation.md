# Documentation Standards

## Docs Are Part Of The Product

`docs/` 和 `mkdocs.yml` 是代码库的一部分。文档错误按代码错误对待。

## Synchronization Rules

出现以下变化时，必须同步更新文档：

- 业务逻辑变化
- 配置项变化
- 公共函数签名变化
- 新增模块、流程或开发规范

## Navigation Maintenance

新增长期文档页时：

1. 创建对应的 Markdown 文件
2. 更新 `mkdocs.yml` 的 `nav`
3. 确保页面在文档站点中可发现

## API Documentation

不要在 Markdown 中手工复制 Python 函数说明。优先使用 `mkdocstrings`：

```markdown
::: backend.core.agent.KnowledgeRetrievalAgent
    handler: python
    options:
      members:
        - execute_task
```

## Markdown Encoding

- 所有 Markdown 文件必须使用 UTF-8 编码
- PowerShell 中读取 Markdown 时使用 `-Encoding utf8`

## Standards Hub Maintenance

如果规范变化与 AI 工作方式相关，优先更新 `docs/ai-standards/`，再同步各工具入口文件。

## Time-Sensitive Knowledge And Evidence

对可能随时间变化的知识，不要只依赖模型记忆或旧文档。遇到以下情况时，在给出结论或执行操作前核验：

- 云服务、软件、协议、API、依赖版本、兼容性、安全建议、价格或服务政策
- 服务器当前配置、运行状态、DNS、网络入口、部署结果和资源使用情况
- 用户明确要求“最新”“当前”“现在”，或结论会影响生产环境与后续操作

优先查阅官方、版本匹配的上游文档；涉及用户自己的环境时，优先做必要的只读实时检查。若无法访问权威来源或实时环境，明确说明结论的时间边界和不确定性，不要把推测写成已验证事实。

记录知识时要区分当前已核验状态、历史快照、推断和计划。对会用于后续运维的私有环境信息，在对应的私有配置中记录核验时间及足以复查的证据；公开文档只写可公开复用的通用做法，不写凭据或不必要的主机私有信息。旧记录是历史证据，不能自动视为当前状态。

按任务触发核验和更新：事实被使用、变更或重新检查时同步更新对应记录；不要为了“保持最新”而无差别、定期重查整个知识库。若项目确实需要周期性巡检，应由用户明确的维护流程定义范围、频率和负责人。
