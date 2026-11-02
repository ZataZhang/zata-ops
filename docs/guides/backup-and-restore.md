# 备份与恢复

## Backup

`zata-ops db backup` 完整流程：

1. 读取项目 `.env` / `.env.local`（CLI flag 优先）。
2. 根据当天 weekday 与 `FULL_BACKUP_DAY` 决定 backup type，`--force-full`
   可强制 full。
3. `pg_dump` / `sqlite3 .dump` → 压缩为 `database.sql.gz`。
4. GNU tar `--listed-incremental` 打包 `LOGS_DIR` 与 `RESOURCES_DIR`。
5. 上传 S3：`<S3_PREFIX>/<YYYY-MM-DD_HHMMSS>/<full|incremental>/<file>`。
6. 写 `manifest.json` 描述本次备份的所有 file + SHA-256 + S3 key + type。
7. 按 `RETENTION_DAYS` 清理 S3 中过期备份目录。

`--dry-run` 在 `_run_backup` 触发任何 IO 之前打印计划，并 mask `DATABASE_URL`
中的密码。CI 推荐用 `--dry-run` 走真实入口验证。

## Restore

```bash
zata-ops db list
zata-ops db restore --from 2026-06-07_180000 \
    --restore-db --restore-logs --restore-resources --yes
```

可选行为：

- `--chain`：对 logs/resources 重放 full + 所有 incremental，确保增量删除
  / 修改全部生效。
- `--clean-target-schema` / `--drop-target-db`：危险操作，需显式确认；
  互斥，不能同时启用。
- `--sanitize-invalid-utf8`：对脏 dump 用 Unicode replacement 字符替换非法
  UTF-8 字节后再 pipe 到 `psql`。
- `--verify-table foo`：restore 后校验 `foo` 表存在且行数 > 0。

## PostgreSQL 跨机迁移

`db migrate` 复制一个 PostgreSQL 数据库的单次快照，不是持续同步，也不是
Alembic schema migration。数据库和登录角色是不同对象：单库 dump 不包含角色、
密码或集群权限。迁移前明确目标数据库名，检查目标集群上同名角色及其登录权限；
不要覆盖已有角色密码。目标库可与源库同名，其他库（例如 `prod_db`）不会自动成为
目标，也不应被覆盖。

### 使用 CLI

```bash
export ZATA_OPS_MIGRATE_SOURCE_URL='postgresql://<source-role>@<source-host>:5432/<source-db>'
export ZATA_OPS_MIGRATE_TARGET_URL='postgresql://<target-role>@<target-host>:5432/<target-db>'
export PGPASSFILE=/run/secrets/postgres-migration.pgpass

zata-ops db migrate --dry-run
zata-ops db migrate --verify-row-counts
```

passfile 应包含源、目标连接条目且权限为 `0600`。避免把含密码的 URL 放进命令行
或 shell 历史。`--dry-run` 只显示计划，不会连接数据库。迁移时，目标库不存在则由
目标 URL 的登录角色创建，该角色需要连接 `postgres` 维护库并拥有 `CREATEDB`，新库
所有者也是该登录角色；目标库已存在时，CLI 通过目标账号检查常见用户对象，发现非空
就拒绝导入，不会清空它。若由管理员提前建库，应把所有者设为目标业务角色，并让目标
登录角色能连接目标库。

命令默认在本机生成权限受限的临时 gzip SQL 转储，结束后删除。`--keep-dump` 可显式
保留它。`--verify-row-counts` 会逐表执行精确 `count(*)`；大库可能耗时较长，源库在
导出后继续写入时也可能出现行数差异。正式切换应在停写窗口导出并核验，或另做经验证
的增量同步。

### 网络路径选择

- 本机能访问两端时，使用 `zata-ops db migrate`。
- 目标库只能经 SSH 访问时，通过已验证的 SSH 隧道连接目标端口。
- 本机链路慢，而目标 PostgreSQL 容器能访问源数据库时，可在目标容器内执行
  `pg_dump -F p | psql --single-transaction`，让 dump 流直接进入目标库。
- 若源库仅能从源容器访问，可从源容器 `pg_dump -Fc`，经 SSH 把归档流送到目标容器
  `pg_restore --exit-on-error`。

目标容器示例应在目标主机（或已指向目标主机的 Docker context）执行。远端容器管道
使用 `bash -o pipefail`，并让 `psql` 设置 `ON_ERROR_STOP=1`；给 `pg_dump` 和 `psql`
使用权限为 `0600` 的 `PGPASSFILE`，不要把密码放进 SSH 或 Docker 参数。确认目标角色、
数据库所有者和空库状态后再导入。

### 验收

经目标实际连接路径登录并执行 `SELECT 1`，核对目标库名和业务账号。比较业务 schema
关系、关键约束、扩展和逐表行数；验证序列、自增写入权限及应用读写。跨 PostgreSQL
大版本时，系统目录内部对象计数可能不同；按业务对象和语义约束比较，不要用 TOAST
等内部对象总数判断迁移是否一致。记录快照时间及源库是否仍有写入；快照不会追上后续写入。

## MySQL 到 PostgreSQL 的分阶段迁移

MySQL 8.0.16+ → PostgreSQL 使用已登记的服务名与本机注入凭据，按
[pgloader MySQL 转换说明](https://pgloader.readthedocs.io/en/latest/ref/mysql.html)处理映射。
本机需要安装 `pgloader`、`pg_dump` 和 `pg_restore`，且 PostgreSQL 客户端版本不能低于目标服务端
主版本；源账号要能读取业务表及视图、触发器、routine、event 的元数据；
目标服务账号须连接维护库、创建数据库、拥有既有目标库，且能
在切换时终止目标库连接。两端凭据通过
`private_secrets.py run --env` 注入；连接串和密码不会作为命令行参数传递。

先查看服务元数据与凭据引用名：

```bash
just service-show freshai-source
just service-show freshai-target
```

把下面的 `SOURCE_PASSWORD_ENV`、`TARGET_PASSWORD_ENV` 和其他 `_PASSWORD_ENV`
占位名替换成 `service-show` 中对应的 `password_env` 值。

先预览，再执行：

```bash
uv run zata-ops db migrate --source-engine mysql \
  --source-service freshai-source --target-service freshai-target --dry-run

uv run python scripts/diagnostics/private_secrets.py run \
  --env SOURCE_PASSWORD_ENV --env TARGET_PASSWORD_ENV -- \
  uv run zata-ops db migrate --source-engine mysql \
    --source-service freshai-source --target-service freshai-target \
    --replace-target
```

目标含有业务对象时必须显式传 `--replace-target`。命令先将目标 PostgreSQL 数据库
备份到 `config/private/backups/db-migration/`，再导入随机命名的临时数据库；它会核对
逐表精确行数，以及主键、外键、CHECK 约束、索引和自增序列数量。所有核验通过后才交换数据库名。原目标
数据库会保留为 `zata_rollback_<随机值>`，以便手工回退；本机归档备份也会保留。请在
源 MySQL 与目标 PostgreSQL 上的应用都停写后执行，并确认本机有空间保存完整目标备份、
PostgreSQL 服务器能同时容纳旧库和临时新库。临时导入或核验失败会删除未提升的临时库，
原目标保持不变。提升后最终行数复核失败时会尝试恢复旧数据库名。
默认 pgloader 时限为一小时；大型数据库可用 `--timeout-seconds` 调高。

本机完整备份包含目标库数据，文件权限为 `600`，保存在被 Git 忽略的
`config/private/backups/db-migration/` 下，不会自动过期；确认迁移和应用读写正常、
且不再需要恢复点后，再手动清理本机备份与 `zata_rollback_<随机值>` 数据库。

迁移成功后如需手动回退，先停应用并用管理员账号连接维护库 `postgres`，逐条执行下列 SQL；
把两个随机数据库名替换为迁移命令输出的回退库名和你选定的失败库名。回退会把当前新库改名保留，
再把旧库恢复成应用使用的数据库名：

```sql
ALTER DATABASE freshai WITH ALLOW_CONNECTIONS false;
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'freshai' AND pid <> pg_backend_pid();
ALTER DATABASE freshai RENAME TO zata_failed_<随机值>;
ALTER DATABASE zata_rollback_<随机值> WITH ALLOW_CONNECTIONS true;
ALTER DATABASE zata_rollback_<随机值> RENAME TO freshai;
ALTER DATABASE freshai WITH ALLOW_CONNECTIONS true;
```

请在 `psql` 中逐条执行，不要包在事务中；恢复后再验证应用数据并决定何时清理失败库。

检查应用读写和回滚备份后，再按维护窗口手工清理旧目标数据库。MySQL 行数核验要求
源表在迁移期间不变；此命令不持续同步后续写入。迁移只覆盖 MySQL 能映射到 PostgreSQL
的对象；未识别的 pgloader 错误、警告、不支持的 CHECK 表达式、未强制 CHECK，以及源库
视图、用户触发器、存储过程/函数或事件都会停止提升。`ON UPDATE CURRENT_TIMESTAMP`
列由 pgloader 转换为 PostgreSQL 更新触发器；迁移后仍需人工核验该行为、字符集、时间类型、
JSON、大小写敏感标识符和业务约束。源 MySQL 账号需要读取全部业务表和对应元数据。

## 创建与轮换 PostgreSQL 应用角色

管理员服务必须已经登记，并通过私有凭据工具注入密码；管理员需要 `CREATEROLE`，并能管理目标库
及 `public` schema 中的对象（通常由目标库 owner 执行）。创建 `freshai_app` 示例：

```bash
uv run python scripts/diagnostics/private_secrets.py run \
  --env ADMIN_PASSWORD_ENV -- \
  uv run zata-ops db role create --admin-service freshai-target \
    --role freshai_app --service-name freshai-app \
    --database freshai --notes "FreshAI runtime application role"
```

命令生成随机 SCRAM 密码，不显示密码，并把新服务记录和凭据保存到本机私有配置。
目标 PostgreSQL 需要 11 或更新版本。
角色不能创建数据库或角色，也没有超级用户、绕过 RLS 或建表权限；它获得 `public`
schema 中现有表的常用 DML、序列使用和 routine 执行权限，并配置管理员后续创建对象的
默认授权。该角色用于应用运行时读写，schema 变更仍通过管理员或单独的迁移身份执行。
如果 `public` schema 仍通过 `PUBLIC` 角色开放 CREATE，创建会停止，避免应用账号继承建表权限；
请先确认该数据库是服务专用库，再调整其 schema 授权并重试。

轮换只需要注入管理员凭据。若旧密码仍在本机私有凭据中，也可一并注入应用服务凭据，
这样在数据库确认仍使用旧密码时，命令可以恢复本机旧值。旧密码已经遗失时，管理员仍可重置：

```bash
uv run python scripts/diagnostics/private_secrets.py run \
  --env ADMIN_PASSWORD_ENV -- \
  uv run zata-ops db role rotate --admin-service freshai-target \
    --service-name freshai-app
```

若旧密码仍有保存，可额外加上 `--env APP_PASSWORD_ENV`。轮换会更新数据库角色密码与本机
私有凭据。随后仍需在应用部署环境更新对应连接密码并重启或重新加载应用；本命令不会连接
或修改 FreshAI 的 Dokploy 配置。若命令报告结果无法确认，新密码仍保存在本机，先核验数据库
上的角色密码再重启应用。

## Manifest 兼容性

`zata-ops` 保留了旧 `backup_service` 的 manifest shape（`timestamp`、
`type`、`files[]` 中的 `name`/`size`/`s3_key`/`sha256`），并在原有字段上
追加了 `project` 字段。旧 manifest 可被 `zata-ops db restore` 直接消费。
