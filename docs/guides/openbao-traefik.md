# Dokploy 的 OpenBao HTTPS API 入口

使用独立 Docker bridge 连接现有 Traefik 和宿主机的 systemd socket proxy。代理将 TCP 流量转发到 OpenBao 已有的 `127.0.0.1:8200`。Traefik 负责公网 TLS 和来源限制，转发时继续校验 OpenBao 的上游证书。对外路由匹配 `/v1/`，管理界面继续通过 SSH 隧道访问。

## 安装

在 OpenBao 宿主机上以 root 执行。使用现有 Traefik v3 的 `websecure` 和 `letsencrypt`，并确认 file provider 将宿主机 `/opt/traefik/dynamic` 挂载为容器内 `/etc/traefik/dynamic`。域名使用直接指向 Traefik 的 DNS 记录。来源填写从 Dokploy 管理容器实际测得的出口 IP，IPv4 用 `/32`、IPv6 用 `/128`。Traefik 的白名单按直接连接的对端地址判断。

```bash
docker network create --driver bridge --internal openbao-ingress
docker network connect openbao-ingress traefik
gateway="$(docker network inspect openbao-ingress --format '{{(index .IPAM.Config 0).Gateway}}')"
```

在现有 Traefik Compose 的 Traefik 服务 `networks` 中追加 `openbao-ingress`，并在顶层 `networks` 声明 `openbao-ingress: {external: true}`，让下次重建保留网络。运行 `docker compose -f /opt/traefik/docker-compose.yml config --quiet` 检查合并结果。当前容器已通过上一步连接，可以直接使用动态配置。

在 OpenBao 主机的 `deploy/openbao/traefik/` 目录以 root 安装 socket relay 和上游证书：

如果宿主机的 UFW 默认拒绝入站，放行专用网桥到本地转发端口。网桥与网段随 Docker 网络而定，重建网络后重新核对。

```bash
subnet="$(docker network inspect openbao-ingress --format '{{(index .IPAM.Config 0).Subnet}}')"
bridge="$(ip -o -4 addr show | awk -v ip="$gateway" '{split($4, address, "/"); if (address[1] == ip) print $2}')"
test -n "$bridge" && ufw allow in on "$bridge" from "$subnet" to "$gateway" port 18200 proto tcp comment "OpenBao Traefik relay"
```

来源名单保存在本机忽略跟踪的 `config/private/openbao/<实例>/ingress.env`。用逗号分隔填写 Dokploy 和所有需要访问 API 的团队出口 CIDR；单个 IPv4 地址写 `/32`，IPv6 地址写 `/128`。团队若有固定 VPN 或 NAT 出口，可登记为一条来源；否则按成员出口逐项维护。新增成员或出口时，只更新此私有清单并重新生成路由，不需要改部署模板。团队成员通过安全渠道取得该实例的私有配置。

```dotenv
OPENBAO_INGRESS_DOMAIN=openbao.example.com
OPENBAO_ALLOWED_SOURCE_CIDRS=192.0.2.10/32,198.51.100.25/32,2001:db8::25/128
```

先按前面的步骤在 OpenBao 主机创建并连接网桥，再从工作站读取网关地址并用私有配置生成路由。生成文件权限为 `0600`，只需将文件复制到主机后安装到 Traefik 的动态配置目录。首次安装前如果发现同名配置已存在，先备份并核对。

```bash
gateway="$(docker network inspect openbao-ingress --format '{{(index .IPAM.Config 0).Gateway}}')"
sed "s|@INGRESS_GATEWAY@|${gateway}|g" openbao-proxy.socket.in \
  > /etc/systemd/system/openbao-proxy.socket
install -m 0644 openbao-proxy.service /etc/systemd/system/openbao-proxy.service
systemctl daemon-reload
systemctl enable --now openbao-proxy.socket
install -m 0644 /opt/zata-ops/openbao/tls/server.crt /opt/traefik/dynamic/openbao-upstream.crt
```

然后在仓库根目录的工作站终端从该主机读取网关，用本机私有配置生成路由并复制到主机。把 `SSH_ALIAS` 替换为实例 SSH 配置中的主机别名，把 `INSTANCE` 替换为私有清单中的实例目录名：

```bash
ssh_alias=SSH_ALIAS
instance=INSTANCE
ssh_config="config/private/openbao/${instance}/ssh.conf"
instance_config="config/private/openbao/${instance}/ingress.env"
gateway="$(ssh -F "$ssh_config" "$ssh_alias" "docker network inspect openbao-ingress --format '{{(index .IPAM.Config 0).Gateway}}'")"
rendered_route="$(mktemp)"
trap 'rm -f "$rendered_route"' EXIT
uv run python deploy/openbao/traefik/render_dynamic.py \
  --config "$instance_config" \
  --gateway "$gateway" \
  --output "$rendered_route"
scp -F "$ssh_config" "$rendered_route" "$ssh_alias:/tmp/openbao-api.yml"
ssh -F "$ssh_config" "$ssh_alias" 'install -m 0644 /tmp/openbao-api.yml /opt/traefik/dynamic/openbao-api.yml && rm -f /tmp/openbao-api.yml'
```

Traefik 使用 `localhost` 匹配现有上游证书的 SAN。OpenBao 换证书后更新 `openbao-upstream.crt`，并让 Traefik 重新加载路由文件。

## 验证和回滚

- 从 Dokploy 管理容器请求 `https://<域名>/v1/sys/health`。应通过公网证书校验，已解封的活动节点返回 HTTP 200。
- 从白名单外请求相同路径应返回 403。从允许的来源请求 `/ui/` 应返回 404。
- 在 Dokploy 的 Secrets Providers 中填写 HTTPS 基础地址和仅可读取目标环境 KV 路径的 token。先测试读取一个验证字段，再为 FreshAI test 配置实际引用。
- 回滚时先移除 `/opt/traefik/dynamic/openbao-api.yml`，再停用 `openbao-proxy.socket` 和 `openbao-proxy.service`。如果添加过 UFW 规则，用相同参数执行 `ufw delete allow`。从 Traefik Compose 移除该网络声明后，断开 Traefik 并删除专用网络。

[Traefik IPAllowList](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ipallowlist/) · [Traefik ServersTransport](https://doc.traefik.io/traefik/reference/routing-configuration/http/load-balancing/serverstransport/) · [Dokploy Secrets Providers](https://docs.dokploy.com/docs/core/secrets-providers)
