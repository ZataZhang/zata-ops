# Keycloak 统一身份中心

模板派生的各个应用共用一个 Keycloak 作为身份中心（IdP）：每个应用在业务 realm 中登记为一个 OIDC Client，用户只维护一个账号；应用继续保留自己的会话、用户表和业务权限，只把“确认用户是谁”交给 Keycloak。

部署文件位于 `deploy/keycloak/`，包含普通 VPS 与 Dokploy 两份 Compose、空白环境变量模板和首次导入用的 realm 配置；不包含服务器地址或真实凭据。安装、凭据、备份与升级步骤见 `deploy/keycloak/README.md`。

## 部署决策

| 决策 | 取值 | 原因 |
|---|---|---|
| 版本 | Keycloak `26.8.2`，PostgreSQL `17.11` | 锁定镜像版本，升级时先读官方升级说明并备份 |
| 节点 | 单节点，`KC_CACHE=local` | 不需要高可用；默认集群缓存会在重启时先尝试加入数据库里登记的旧节点，拖慢启动 |
| TLS | 在 Traefik 终止，Keycloak 内部走 HTTP 8080 | `KC_HTTP_ENABLED=true` 与 `KC_PROXY_HEADERS=xforwarded` 配合反向代理 |
| 公网路径 | 只放行 `/realms/<realm>/` 与 `/resources/` | master realm、`/admin/`、管理端口 9000 不经过代理 |
| 管理后台 | 绑定 `127.0.0.1:18180`，经 SSH 隧道访问 | 与 nacos 的 Console 一致；端口避开本机 nacos 开发实例占用的 18080 |
| 数据库 | 独立 PostgreSQL，绑定 `127.0.0.1:15432` | 账号数据与业务库隔离；回环端口供 `zata-ops db backup` 使用 |

环境变量 `KC_PUBLIC_HOST` 只填主机名，约定为 `auth.<根域名>`。SMTP 暂未配置，因此 realm 关闭了自助注册、邮箱验证与找回密码，账号由管理员创建。

## realm 结构

`realm/zata-realm.json` 定义业务 realm `zata`：

- 团队用组表示，放在 `/teams` 下，例如 `/teams/platform`。
- 应用内能做什么用客户端角色表示，例如 `freshai-dev` 的 `user` 与 `admin`；角色授给团队组，用户随组获得角色。
- 每个应用的客户端带一个角色映射器，把本应用的角色写进 ID Token 的 `resource_access.<client>.roles`，应用登录回调直接读取。Keycloak 默认只把客户端角色写进 Access Token。
- 客户端强制 PKCE（S256），关闭隐式流程与密码直连授权。
- 启用暴力破解防护，密码至少 12 位。

realm 文件只在 realm 不存在时导入，之后的改动先改文件并审查，再用 `kcadm.sh` 应用到线上，步骤见 README。

## 新应用接入

1. 在 realm 文件中为应用的每个环境新增一个 Client：精确的回调地址、强制 PKCE、`user`/`admin` 客户端角色和 ID Token 角色映射器；审查后用 `kcadm.sh` 应用到线上。
2. 把角色授给需要访问该应用的团队组。
3. 应用侧配置 issuer（`https://<KC_PUBLIC_HOST>/realms/zata`）、Client ID 与 Client Secret；Client Secret 只放在部署环境。
4. 应用用 `(iss, sub)` 映射本地用户，不用邮箱当主键；没有 `user` 角色的用户拒绝登录。

## 验证记录

本机按 `compose.yaml` 启动（Docker 网段已耗尽，测试时两个容器改挂已有网络，不改 Keycloak 配置）：

- realm 首次启动导入成功，重启后跳过导入，测试用户保留。
- 发现文档、JWKS 可访问；生产 hostname 配置下 issuer 为 `https://<KC_PUBLIC_HOST>/realms/zata`。
- 经真实登录页跑通授权码 + PKCE：`/teams/platform` 成员的 ID Token 带 `admin`、`user` 角色，无组用户不带角色。
- 负控：未登记的回调地址返回 `Invalid parameter: redirect_uri`；不带 PKCE 的请求被拒绝（`Missing parameter: code_challenge_method`）。
- 恢复演练：`pg_dump` 导出后恢复到新数据卷，Keycloak 在新卷上启动，原有用户与登录流程均正常。
- `KC_CACHE=local` 之前，重启后的容器会先尝试加入上一个容器登记的集群节点，约 20 秒后才放弃；改为单节点缓存后，重建容器约 11 秒就绪。

未在本机验证：Traefik 路径过滤、Let's Encrypt 证书与 Dokploy 部署，需在目标服务器上线时确认。
