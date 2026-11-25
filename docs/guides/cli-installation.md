# 独立安装与本机诊断

有版本标签后，可在任意目录用 uv 安装固定版本。将 `<VERSION_TAG>` 换成仓库中已发布的标签。

```bash
uv tool install 'git+https://github.com/ZataZhang/zata-ops.git@<VERSION_TAG>'
zata-ops --version
```

也可从 GitHub Actions 的 CLI wheel 工作流下载已验收的 wheel，解压后用
`uv tool install /path/to/zata_ops-<version>-py3-none-any.whl` 安装。
该工作流在拉取请求、版本标签推送或手动触发时构建 wheel，在仓库外的隔离环境中验证实际命令。
固定标签锁定 CLI 源码版本，Python 依赖按该版本声明的约束解析。

## 从仓库外读取私有配置

按[私有运维配置](private-inventory.md)和[服务与凭据管理](private-secrets.md)准备文件。
已有配置可以继续使用原目录。独立目录应仅对当前用户可读，查询命令不会修改文件或权限。

```bash
export ZATA_OPS_PRIVATE_DIR='/path/to/private'
zata-ops doctor
zata-ops service list
zata-ops service show main-postgres
zata-ops app inspect example-app --server example-server --dry-run
```

`doctor` 和 `service list/show` 接受 `--private-dir`，优先于 `ZATA_OPS_PRIVATE_DIR`。
`app inspect` 优先使用 `--inventory`，其次使用环境变量指定目录里的 `servers.toml`。
均未指定时，从当前目录下的 `config/private/` 读取。

新命令将单份 JSON 写入标准输出，包含 `schema_version`、`operation` 和 `status`。
参数语法错误由 Typer 报告。

- `doctor` 报告 CLI 版本、配置目录、清单解析结果和 PATH 中的 `ssh`。缺少可选的 `services.toml` 仍可通过。
  本机检查通过时退出码为 0，检查失败时为 1。远程检查使用 [`app inspect`](application-inspection.md)。
- `service list/show` 返回 `services` 数组，包含服务元数据、凭据是否配置及过期状态。已存在的私有目录缺少
  `services.toml` 时视为空清单，缺少 `secrets.env` 时显示凭据未配置。退出码 0 表示成功，1 表示名称未找到，
  2 表示配置无效或不可读。返回的服务元数据仍属于私有信息。
