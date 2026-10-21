# 运维配置

本目录区分可共享的配置约定与仅保存在本机的真实运维信息。

```text
config/
├── README.md                  # 配置入口说明，可提交
├── servers.example.toml       # 虚构服务器示例，可提交
├── services.example.toml      # 虚构服务示例，可提交
├── secrets.env.example        # 虚构凭据格式示例，可提交
└── private/                   # 真实配置，整个目录被 Git 忽略
    ├── servers.toml           # 服务器、SSH 引用和应用部署清单
    ├── services.toml          # 服务地址、用户名、用途及凭据引用
    ├── secrets.env            # 密码、Token 和带凭据的连接串
    ├── backups/               # 最近 20 份本机备份
    └── .copy-reminder         # 用户手动复制配置的确认时间
```

`private/` 中还可以保存服务器盘点快照等私有记录；Git clone 不包含这些文件。
示例只使用虚构值，禁止把真实地址、账号或密码填入示例文件。

## 首次配置

在仓库根目录运行（已存在的配置不要覆盖）：

```bash
uv sync
mkdir -p config/private
cp -n config/servers.example.toml config/private/servers.toml
chmod 700 config/private
chmod 600 config/private/servers.toml
```

编辑 `config/private/servers.toml`，替换成自己的服务器后执行 `just inventory-check`。
服务清单和凭据文件无需手动创建，使用下面的服务登记向导自动生成。
`secrets.env.example` 仅用于说明凭据格式，无需复制；其中的虚构值不能用于真实连接。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `just inventory-check` | 校验服务器清单及 Git 私有性，并检查复制提醒 |
| `just service-add` | 引导填写服务连接信息，隐藏输入密码并自动关联 |
| `just service-list` | 查看服务连接信息和密码是否已配置，不显示密码 |
| `just service-show main-postgres` | 查看指定服务，名称替换成自己的服务名 |
| `just secret-set TOKEN_NAME` | 独立存储凭据，用户在本机终端隐藏输入 |
| `just secret-list` | 只列凭据名称 |
| `just private-backup` | 修改前创建本机备份，保留最近 20 份 |
| `just private-backup-reminder` | 检查是否需要手动复制配置到其他位置 |
| `just private-copy-done` | 用户实际完成复制后记录时间，7 天后再次提醒 |

`service-add` 只登记服务，不会创建远端账号或测试连接。
Agent 使用密码时按服务名称注入可信子进程，具体命令见下方指南；不要打印凭据文件。

## 保存与交接约定

- 修改已有私有配置前先备份；服务和凭据写入脚本会自动备份。
- 私有目录权限 `700`，配置和备份文件权限 `600`；这些文件未加密。
- 本机备份也包含密码和历史密码，手动复制时按凭据文件保管。
- 提醒仅在运行相关命令时检查，没有后台定时任务；确认命令本身不复制文件。
- 真实配置通过自己信任的安全渠道交接，新机器需调整 SSH 私钥路径。
- URL 中含密码或 Token 时属于凭据；服务元数据只保存不带凭据的地址。

详细字段和恢复说明见[私有运维配置](../docs/guides/private-inventory.md)，
服务登记与凭据使用见[服务与凭据管理](../docs/guides/private-secrets.md)。
