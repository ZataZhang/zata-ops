# PostgreSQL 跨机迁移

此流程用于把一个数据库复制到另一台服务器。若用户要正式迁移应用，还需单独完成停写、最终复制、连接配置切换和应用验证；源库不因复制成功而自动删除。

## 先核对

1. 明确源库、目标服务器、**目标库名**以及目标是否已存在。已有数据的目标库不能直接导入；覆盖或删除需要明确范围和可恢复备份。
2. 只读检查两端 PostgreSQL 版本、源库大小、schema、扩展、表及对象所有者，确认目标空间和所需角色。检查源应用是否持续写入，并约定快照与正式切换的时间点。
3. 检查实际网络路径：直连能否用、是否需要 `zata-ops tunnel`、数据库只在主机还是 Docker 容器内可达。SSH 登录成功不代表隧道后端可用；用数据库账号执行只读查询验证。
4. 核对 `pg_dump`、`pg_restore` 或 `psql` 的位置和版本。优先使用兼容目标服务端版本的客户端工具；目标比源旧时先评估兼容性。不要仅因仓库依赖 `psycopg` 就假定本机装有 PostgreSQL CLI。

## 数据库名与角色

PostgreSQL 数据库和登录角色是两类独立对象。单库 `pg_dump` 不会复制角色、密码或集群级权限。用户要求“按旧库同名同密码复制”时，先检查目标集群是否已有同名角色并验证登录；不要为图省事覆盖已有角色密码。若需新建角色，应作为单独的集群级操作处理，并使用受限凭据输入。

目标库名可以和源库同名，也可以不同；先明确用户要求的名称。若目标上已有其他数据库（例如 `prod_db`），不能据此把它当成目标库，也不能覆盖它。

## 选择迁移路径

### `zata-ops db migrate`

`zata-ops db migrate --source <postgresql-url> --target <postgresql-url> --dry-run` 可显示执行计划；dry-run 不连接数据库，也不验证计划。去掉 `--dry-run` 后，它在本机用 `pg_dump` 生成 gzip SQL，再用 `psql` 导入目标。它要求本机有 PostgreSQL CLI，源和目标都可从本机访问。目标只经 SSH 可达时，先开隧道，再将目标 URL 的 host/port 指向本机转发端口。也可通过 `ZATA_OPS_MIGRATE_SOURCE_URL` 和 `ZATA_OPS_MIGRATE_TARGET_URL` 环境变量传 URL。

该命令不会创建业务角色，也不会停止应用写入。目标库缺失时，命令会用目标 URL 中的登录角色创建，因而该角色需要连接 `postgres` 维护库并拥有 `CREATEDB`；新库所有者就是该登录角色。已有目标库必须为空，命令会检查常见 schema 对象、关系、函数、类型和 large objects；发现对象就拒绝导入，不会清空数据库。若由管理员预建目标库，应把所有者设为目标业务角色，并使用目标业务账号完成迁移。

可选 `--verify-row-counts` 会在导入后逐表执行精确 `count(*)` 并比较源和目标；它可能耗时较长，源库在导出后仍有写入时也可能报差异。可选 `--keep-dump` 保留本地 SQL 转储；默认转储文件以仅当前用户可读写的权限创建，并在命令结束时删除。连接 URL 应使用 `postgresql://`，不要传 SQLAlchemy 的 `postgresql+psycopg2://`。不要把密码直接写进命令行参数或 shell 历史；用不含密码的 URL 配合权限为 `0600` 的 `.pgpass` / `PGPASSFILE`，或受限的环境变量。

### 数据库运行在远端 Docker 中

按网络可达方向选择执行位置：

- 若目标 PostgreSQL 容器能连接源数据库，在目标容器内运行源端 `pg_dump`，并把 SQL 管道直接交给目标端 `psql`。这样不会先把转储搬到操作员电脑。在目标主机（或已指向目标主机的 Docker context）运行下例；主机、库名和用户按环境替换，容器内的 `PGPASSFILE` 应同时包含源和目标连接条目：

  ```bash
  docker exec -i <target-postgres-container> bash -o pipefail -c '
    export PGPASSFILE=/run/secrets/migration.pgpass
    pg_dump -w -h <source-host> -p 5432 -U <source-role> -d <source-db> -F p |
      psql -X -w -v ON_ERROR_STOP=1 --single-transaction \
        -h 127.0.0.1 -p 5432 -U <target-role> -d <target-db>
  '
  ```

- 若源数据库只能从源端容器访问，可在源容器执行 `pg_dump -Fc`，通过 SSH 把归档流传给目标端容器的 `pg_restore --exit-on-error`。

两条容器路径都要先确认目标角色和**空目标库**的所有者，再导入。使用 `bash -o pipefail` 检查管道两端退出码；`pg_dump -w` / `psql -w` 禁止交互式密码提示，`psql -v ON_ERROR_STOP=1` 遇到 SQL 错误即停止。不要把密码放入 SSH argv、Docker argv 或日志；传输结束后清理临时 passfile。

`pg_dump` 提供一致的单次快照，但源库在快照后继续写入时，目标不会自动追上。正式切换时先停写并做最终复制，或设计经验证的增量同步方案。不要在已有数据的目标库中直接重复恢复快照。

## 验收与收尾

- 用目标业务账号经实际连接路径登录，执行 `SELECT 1`；确认库名、账号与目标主机。
- 对比源和目标 schema、表、索引及关键约束，逐表比较行数；业务重要表可补充校验和或抽样核对。持续写入时，源库当前行数可能与快照不同，应按切换窗口解释差异。
- 验证序列、自增写入权限和应用真实读写流程；跨 PostgreSQL 大版本时，系统目录内部计数表示法可能变化，应按业务 schema 对象和约束类型比较。不要把 TOAST 等内部对象或系统目录中的原始行数当成业务一致性依据。
- 报告快照时间、实际导入及核对结果、源库是否仍在写入、应用是否已切换。关闭临时隧道，清理含敏感数据的临时转储文件；保留源库直到切换和回退窗口结束。
