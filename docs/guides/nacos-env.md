# Nacos dotenv 安全使用

Nacos 配置类型选择 `text`，正文遵循 dotenv 语法。此工具只读远端，不发布或修改配置。
只列变量名，或把选中的值注入可信子进程；原始正文只在工具内存中处理，不保存成本地文件。

## 准备凭据

在本机交互终端分别执行，隐藏输入 Nacos 用户名和密码：

```bash
just secret-set NACOS_USERNAME
just secret-set NACOS_PASSWORD
```

用户名也保存在本地凭据库，以便工具自行登录。使用专用普通账号，仅授予需要配置的读取权限。
也可保存已有 access token，使用 `--token-ref NACOS_ACCESS_TOKEN` 替代用户名密码登录；
工具不刷新该 token，到期后需更新。可通过 `--username-ref`、`--password-ref` 选择其他本地凭据名。

## 列名称与注入

下面地址、配置名均为虚构示例。`--server` 使用 Client API 地址，不是独立 Console 地址，
允许根地址或以 `/nacos` 结尾的地址。生产连接使用 HTTPS 或受信任的本地隧道。

```bash
just nacos-env --server https://nacos.example.com --data-id app.env keys

just nacos-env --server https://nacos.example.com --namespace dev --group DEFAULT_GROUP --data-id app.env run --env DB_HOST --env DB_PASSWORD --cwd ../my-app -- uv run python check_database.py
```

`keys` 只输出排序后的键名，包括空值和未赋值的键。`run` 必须用重复的 `--env` 显式选键；
变量不存在或未赋值时拒绝执行，`NAME=` 则以空字符串注入。选定变量覆盖继承环境中的同名变量；
其他父进程环境照常继承。工具内部读取的 Nacos 登录凭据不会额外注入子进程。

解析不展开 `${...}`，支持 dotenv 引号、注释和引号内多行值。名称遵循现有凭据约定：
大写字母开头，仅含大写字母、数字和下划线。拒绝重复键、非法语法、NUL、非 text 或加密配置；
请求限制 15 秒、响应限制 1 MiB，拒绝 HTTP 重定向，错误不回显正文或原始异常。

默认屏蔽子进程 stdout、stderr 和交互输入，只返回退出状态；没有 `--show-output`。
失败返回 `1`，子进程正常退出时返回其退出码，信号终止归一为 `1`。
登录凭据、token、配置值不会写入命令参数。

这与本地凭据工具一样用于防止意外回显，不是强制访问隔离：可信子进程能使用变量值，
也可能将其写入文件或发送到网络；同一用户仍可能直接读取凭据并访问 Nacos。
原始值不进入模型上下文的前提是通过此入口操作，不另行读取原始配置、日志或文件。

## 验证与协议依据

离线集成验证：`uv run pytest tests/test_nacos_env.py tests/test_private_secrets.py tests/test_private_services.py`。
测试用本地 HTTP 替身模拟 Nacos 登录和配置接口，通过真实 CLI 启动真实子进程验证注入与输出隐藏；
不连接私有 Nacos，不使用真实凭据。真实集群可用上方 `keys` 命令只读验收。

2026-11-09 核对官方 [Nacos 3.2 Client API](https://www.nacos.io/en/docs/v3.2/manual/user/open-api/)
及[访问凭据](https://nacos.io/en/docs/latest/manual/user/auth/)：使用 v3 登录接口和配置读取接口，
access token 放请求头，不放 URL。工具适用于提供这些接口的 Nacos 3.x 默认鉴权。
