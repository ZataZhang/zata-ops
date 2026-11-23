# OpenBao 部署与使用

`deploy/openbao/` 提供单节点 OpenBao，使用 Raft 持久化和独立审计卷。它适合经 SSH 管理的私有实例。使用它保存 API Key、数据库密码等凭据，Nacos 可以继续发布业务参数和开关。主机重启后，采用 Shamir 解封的实例需要重新解封。对恢复时间有要求的应用应先评估备份、恢复和高可用方案。

个人账号、权限、登录、token 续期、密钥更新与故障排查见 [OpenBao 日常使用指南](openbao-usage.md)。

## 部署

从[私有清单](private-inventory.md)获取目标主机的 SSH 参数。在 Linux 主机上确认 Docker Engine 28 或更新版本、Compose 插件、OpenSSL 和宿主机回环端口可用。镜像固定到 `2.7.2` 和对应的多架构摘要。

将 `deploy/openbao/` 复制到目标目录，在该目录中以 root 运行以下命令。生成的 TLS 私钥留在宿主机，容器通过只读挂载访问。

```bash
docker compose config --quiet
docker compose pull
sh prepare-tls.sh
docker compose up -d
```

服务只发布宿主机的 `127.0.0.1:8200`。管理通过授权的 SSH 隧道访问，Raft 集群端口留在容器内。

Dokploy 需要跨主机读取密钥时，可以在 OpenBao 所在主机的 Traefik 添加仅允许 Dokploy 来源的 HTTPS API 入口。安装、连通性验证和回滚见 [Traefik 入口配置](openbao-traefik.md)。

## 从工作站连接

通过已验证的 SSH 会话将远端 `tls/server.crt` 复制到本机私有目录。它是客户端需要信任的**公开证书**。下方示例中的 SSH 目标代表已配置的 SSH 主机别名，也可换成私有清单里的登录信息。

```bash
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes \
  -L 127.0.0.1:8200:127.0.0.1:8200 openbao-host
```

在另一个本机终端连接：

```bash
export BAO_ADDR=https://127.0.0.1:8200
export BAO_CACERT=/path/to/private/openbao/server.crt
bao status
```

首次启动时健康接口 `/v1/sys/health` 返回 `501`（未初始化）；初始化后仍封存时返回 `503`；解封后活动节点返回 `200`。远端 `docker compose ps` 显示的健康状态也以解封为准。控制台地址为 `https://127.0.0.1:8200/ui/`，浏览器需要信任同一张证书。更换证书时先通过验证过的 SSH 通道把新的公钥证书分发给客户端。[健康接口](https://openbao.org/docs/api/system/health/)，[控制台和 TLS](https://openbao.org/docs/configuration/ui/)

## 首次初始化和登录

在受控终端初始化一次。先确定归档的加密接收者和保管方式，再生成解封份额和初始 root token。用已有 SSH 公钥加密给本机也是一种选择。

```bash
set -o pipefail
set -o noclobber
umask 077
export BAO_CLIENT_TIMEOUT=120s
bao operator init -key-shares=3 -key-threshold=2 -format=json \
  | age -R /path/to/backup-recipients.txt > bootstrap.json.age
```

用对应私钥确认归档可解密。Shamir 份额宜分开保管。运行两次 `bao operator unseal` 并在提示中输入不同的份额。后续节点重启时只需要解封。若初始化超时，先检查健康接口。初始化可能已写入 Raft，继续操作前应确认是否成功取得本次解封材料。[OpenBao 初始化](https://openbao.org/docs/commands/operator/init/)，[解封流程](https://openbao.org/docs/concepts/seal/)

首次解封后，通过交互式 `bao login -no-print` 登录初始 root token，启用 KV v2 和应用认证，安装随部署提供的策略：

```bash
bao secrets enable -path=kv kv-v2
bao auth enable approle
bao auth enable userpass
bao policy write openbao-operator deploy/openbao/operator-policy.hcl
```

在 Bash 中输入高强度密码并创建个人运维账号。命令通过 stdin 提交密码，登录生成的 token 有两小时的续期上限。

```bash
read -r -s -p 'Operator password: ' openbao_password
printf '\n'
printf '%s' "$openbao_password" | bao write auth/userpass/users/operator \
  password=- token_policies=openbao-operator token_ttl=30m token_explicit_max_ttl=2h
unset openbao_password
```

在独立会话验证 Userpass 账号能登录并访问 KV，回到初始 root 会话执行 `bao token revoke -self`。运维账号可管理 KV、创建 `app-` 前缀的 ACL 和 AppRole、导出 Raft 快照。由于能编写应用策略，应将该账号视为受信任的管理员。应用使用各自的读取策略。团队接入统一身份时可以改用 OIDC。

## 写入与读取密钥

登录 Web UI 后，在 `kv` 下创建例如 `prod/example-app/runtime` 的记录，填写 `DATABASE_PASSWORD`、`API_KEY` 等字段。临时查看某个字段时，可以在受控终端登录再读取：

```bash
bao login -method=userpass -no-print username=operator
bao kv get -mount=kv -field=DATABASE_PASSWORD prod/example-app/runtime
```

KV v2 的策略使用 API 路径。给 `example-app` 的生产角色只授予所需路径：

```hcl
path "kv/data/prod/example-app/runtime" {
  capabilities = ["read"]
}
```

为每个应用和环境创建独立 AppRole，把读取策略绑定到角色，并由可信的部署流程提供短时 SecretID。新主机先建立可信连接，再凭角色登录。跨主机长期使用时可按应用的网络位置选择受控入口。对于只在启动时读取环境变量的应用，更新 KV 后需重新部署或重启才能使用新值。[KV v2 命令](https://openbao.org/docs/commands/kv/)，[AppRole](https://openbao.org/docs/auth/approle/)

## 停止、备份与维护

- `docker compose down` 停止实例，保留 Raft 和审计卷。重新启动后从原卷解封。`down --volumes` 会删除这两个卷。
- 如需从空卷启动并保留现有卷，在部署目录的私有 `.env` 中设置 `OPENBAO_RAFT_VOLUME` 为一个尚不存在的卷名，再重建容器。新卷需要独立初始化。切换回原卷时使用原来的解封份额。
- 通过已登录的工作站运行 `bao operator raft snapshot save openbao.snap`，将快照加密后存放到独立位置，并定期在隔离实例验证恢复。恢复需要原数据对应的解封材料。[Raft 快照指南](https://openbao.org/docs/api/system/storage/raft/)
- `/openbao/logs/audit.jsonl` 位于命名卷，容器重建后仍然保留。监控卷所在磁盘的空间。若使用归档式轮换，归档后给 OpenBao 进程发送 `SIGHUP` 以便重新打开日志文件。[文件审计设备](https://openbao.org/docs/audit/file/)
- 自签名证书的有效期为一年。提前准备新证书并更新客户端信任，随后重新加载服务端证书或在维护窗口重启再解封。[TCP listener 配置](https://openbao.org/docs/configuration/listener/tcp/)
- 运维账号丢失时，有宿主机访问权的恢复人员可在受控维护窗口临时将监听器的 `disable_unauthed_generate_root_endpoints` 设为 `false`，重建容器并解封，用达到门限的份额执行 `bao operator generate-root`。恢复运维账号、撤销临时 root token 后恢复默认监听器设置。[根令牌生成接口说明](https://openbao.org/community/deprecation/unauthed-generate-root/)，[generate-root 命令](https://openbao.org/docs/commands/operator/generate-root/)

官方资料于 2026-11-20 核验。[OpenBao 2.7.2](https://github.com/openbao/openbao/releases/tag/v2.7.2)，[容器安装](https://openbao.org/docs/install/)，[声明式审计](https://openbao.org/docs/configuration/audit/)，[Docker 端口发布](https://docs.docker.com/engine/network/port-publishing/)
