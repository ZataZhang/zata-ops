# 服务与凭据管理

服务位置和用途保存在 `config/private/services.toml`；密码、Token 和带凭据的连接串存放在
`config/private/secrets.env`。日常使用服务登记向导，自动保存二者的关联。
`config/services.example.toml` 是虚构示例；服务脚本会读取并校验此结构，其他运维命令尚未自动读取服务清单。
`config/secrets.env.example` 提供虚构凭据格式，并与服务示例中的 `password_env` 对应；
无需复制，登记向导会自动生成私有凭据文件。

凭据脚本采用本机明文 dotenv 文件，目录权限 `700`、文件权限 `600`，要求文件被 Git 忽略且未被跟踪。
这适合个人本机使用，不是加密保险库，也不是阻止同一用户或 Agent 读取文件的访问控制边界。

## 添加与查询服务

用户在本机终端执行：

```bash
just service-add
```

依次填写服务名称、类型、所属服务器、无凭据的 URL（可留空）、主机地址、端口、数据库名、
用户名和用途。已登记的服务器名称会显示出来，外部服务的所属服务器留空。
URL 可辅助填写主机和端口，拒绝含账号密码、查询参数或片段的 URL。
PostgreSQL、MySQL 和 Redis 预填常用端口。URL 不替代主机、数据库和用户名字段。

随后隐藏输入两次该服务**已有的密码或 Token**，确认后保存。不会创建或修改远端数据库账号，
也不会测试服务连接。凭据名称自动生成，不需要用户记住；名称重复时拒绝覆盖。
无密码服务暂不支持此向导，可继续手动管理其元数据。

```bash
just service-list
just service-show main-postgres
```

显示地址、端口、用户名、用途及 `password_configured` 状态，不显示密码。
空清单会提示添加服务；非法配置的错误不会回显原始值。

按服务名称直接使用关联密码，例如为 PostgreSQL 客户端注入 `PGPASSWORD`：

```bash
uv run python scripts/diagnostics/private_services.py run --password-env PGPASSWORD main-postgres -- psql -h db.example.com -U app_user -d app -c 'SELECT 1'
```

省略 `--password-env` 时注入 `SERVICE_PASSWORD`。地址、用户名等仍需在客户端命令中明确指定，
脚本不会拼接带密码的 URL。执行行为和输出屏蔽规则与下方底层凭据工具相同。

保存前自动备份已有私有配置，单个文件采用原子替换，面向单用户串行操作。
两个文件不具备跨文件事务：若保存中途发生磁盘故障，密码可能已存入但服务未登记；
请根据备份核对恢复，脚本不会输出密码。

## 底层凭据工具

只需要单独存储 Token 或完整连接串时使用以下命令；登记数据库优先用 `just service-add`。

用户在自己的交互终端执行：

```bash
just secret-set MAIN_POSTGRES_PASSWORD
```

按提示隐藏输入密码，不会显示或作为命令行参数传递。名称必须由大写字母、数字、下划线组成，
以字母开头；支持密码、Token 和完整连接串，不支持空值、多行值或 NUL。
重复设置同名凭据会更新其值。写入前自动复用现有私有配置备份，备份失败则停止；首次空目录无需备份。
写入采用临时文件替换，保留其他凭据。当前工具面向单用户串行操作，不支持并发编辑。

自动化可从受信任的凭据源通过管道传入：

```bash
trusted-secret-source | uv run python scripts/diagnostics/private_secrets.py set MAIN_POSTGRES_PASSWORD --stdin
```

其中 `trusted-secret-source` 是占位命令，需要换成自己的凭据来源；不要用包含真实密码的
`echo`、命令行参数、聊天消息或工具调用文本。Agent 应让用户本机执行隐藏输入命令。
没有交互终端时默认输入失败，不会退回明文回显。

## 列出与使用

```bash
just secret-list
```

只列名称，不显示值。没有“打印密码”的 get/show 命令。
取出是把指定凭据直接注入子进程环境：

```bash
uv run python scripts/diagnostics/private_secrets.py run --env MAIN_POSTGRES_PASSWORD -- your-client-command
```

将 `your-client-command` 替换为能读取该环境变量的实际客户端命令；`--env` 可重复指定，
环境变量名称保持不变，不会自动变成数据库客户端专用变量。
运行前确认命令的操作范围，脚本会实际执行命令，无 shell 插值、无执行预览。
默认屏蔽子进程 stdout、stderr 和交互输入，只显示退出状态，并返回子进程退出码；输入/存储错误返回 `1`。
不直接使用 `.env` 的变量展开规则，密码中的 `${...}` 保持原值。

只运行可信命令：目标进程能够读取注入值，仍可能写文件或通过网络发送凭据。
屏蔽输出用于减少意外回显，不是沙箱。诊断日志、报告、对话和命令预览中不得输出凭据。

## 备份

现有 `just private-backup` 会包含 `secrets.env`，历史备份可能保留旧密码。
压缩包**未加密**，手动复制时也要按凭据文件保管，放到自己信任的加密存储位置。
备份、提醒与恢复方式见[私有运维配置](private-inventory.md)。
