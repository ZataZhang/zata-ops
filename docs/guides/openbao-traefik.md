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

如果宿主机的 UFW 默认拒绝入站，放行专用网桥到本地转发端口。网桥与网段随 Docker 网络而定，重建网络后重新核对。

```bash
subnet="$(docker network inspect openbao-ingress --format '{{(index .IPAM.Config 0).Subnet}}')"
bridge="$(ip -o -4 addr show | awk -v ip="$gateway" '{split($4, address, "/"); if (address[1] == ip) print $2}')"
test -n "$bridge" && ufw allow in on "$bridge" from "$subnet" to "$gateway" port 18200 proto tcp comment "OpenBao Traefik relay"
```

在 `deploy/openbao/traefik/` 对应的目标目录替换下列示例值。已登记域名从本机忽略跟踪的 `config/private/openbao/` 下对应实例的 `ingress.env` 获取。首次安装前如果发现同名配置已存在，先备份并核对。

```bash
domain=openbao.example.com
dokploy_source_cidr=192.0.2.10/32
upstream_cert=/opt/zata-ops/openbao/tls/server.crt

sed "s|@INGRESS_GATEWAY@|${gateway}|g" openbao-proxy.socket.in \
  > /etc/systemd/system/openbao-proxy.socket
install -m 0644 openbao-proxy.service /etc/systemd/system/openbao-proxy.service
systemctl daemon-reload
systemctl enable --now openbao-proxy.socket

install -m 0644 "$upstream_cert" /opt/traefik/dynamic/openbao-upstream.crt
route_file="$(mktemp /opt/traefik/dynamic/.openbao-api.XXXXXX)"
sed -e "s|@INGRESS_GATEWAY@|${gateway}|g" \
  -e "s|@DOMAIN@|${domain}|g" \
  -e "s|@DOKPLOY_SOURCE_CIDR@|${dokploy_source_cidr}|g" \
  openbao-dynamic.yml.in > "$route_file"
chmod 0644 "$route_file"
mv "$route_file" /opt/traefik/dynamic/openbao-api.yml
```

Traefik 使用 `localhost` 匹配现有上游证书的 SAN。OpenBao 换证书后更新 `openbao-upstream.crt`，并让 Traefik 重新加载路由文件。

## 验证和回滚

- 从 Dokploy 管理容器请求 `https://<域名>/v1/sys/health`。应通过公网证书校验，已解封的活动节点返回 HTTP 200。
- 从白名单外请求相同路径应返回 403。从允许的来源请求 `/ui/` 应返回 404。
- 在 Dokploy 的 Secrets Providers 中填写 HTTPS 基础地址和仅可读取目标环境 KV 路径的 token。先测试读取一个验证字段，再为 FreshAI test 配置实际引用。
- 回滚时先移除 `/opt/traefik/dynamic/openbao-api.yml`，再停用 `openbao-proxy.socket` 和 `openbao-proxy.service`。如果添加过 UFW 规则，用相同参数执行 `ufw delete allow`。从 Traefik Compose 移除该网络声明后，断开 Traefik 并删除专用网络。

[Traefik IPAllowList](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ipallowlist/) · [Traefik ServersTransport](https://doc.traefik.io/traefik/reference/routing-configuration/http/load-balancing/serverstransport/) · [Dokploy Secrets Providers](https://docs.dokploy.com/docs/core/secrets-providers)
