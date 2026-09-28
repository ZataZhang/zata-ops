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

## Manifest 兼容性

`zata-ops` 保留了旧 `backup_service` 的 manifest shape（`timestamp`、
`type`、`files[]` 中的 `name`/`size`/`s3_key`/`sha256`），并在原有字段上
追加了 `project` 字段。旧 manifest 可被 `zata-ops db restore` 直接消费。
