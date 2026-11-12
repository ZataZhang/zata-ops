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
| 管理后台 | 绑定 `127.0.0.1:18180`，经 SSH 隧道访问；首次部署后把 master realm 的 Frontend URL 设为该隧道地址（命令见 README） | 与 nacos 的 Console 一致；端口避开本机 nacos 开发实例占用的 18080 |
| 数据库 | 独立 PostgreSQL，绑定 `127.0.0.1:15432` | 账号数据与业务库隔离；回环端口供 `zata-ops db backup` 使用 |

环境变量 `KC_PUBLIC_HOST` 只填主机名，约定为 `auth.<根域名>`。共享 Keycloak 登录页由 `shared-otp-authenticator` 提供密码、邮箱验证码和短信验证码入口；SMTP 配置在 Keycloak 管理后台（Realm settings → Email，保存在 Keycloak 数据库），阿里云短信凭据只配置在 Keycloak 容器。各应用只保存自己的 OIDC client secret，并在 callback 时按自身注册策略创建本地账号。

## 环境划分

| 环境 | Keycloak | 说明 |
|---|---|---|
| 本地开发 | `just keycloak-local` 启动的本机实例与 Mailpit | 与线上同镜像、同 realm 模板；验证码邮件进 Mailpit，不发短信 |
| 自动化测试 | 应用自带的本地 OIDC 替身 | 不依赖外部服务 |
| 测试环境 | 生产实例上的独立 realm（如 `test-dewell`） | 用户与配置隔离；插件和主题与生产共用，Keycloak 改动先在本地验证 |
| 生产 | 业务 realm | — |

测试环境不要直接接入生产 realm：测试应用会拿到真实用户的身份信息，测试账号也会混入生产用户。需要正式预发布 Keycloak 改动时，再把测试环境换成独立实例。

## realm 结构

`realm/realm.json` 定义业务 realm，名称取自必填变量 `KC_REALM`，没有默认值。Keycloak 要求导入文件名为 `<realm>-realm.json`，所以 compose 按 `KC_REALM` 命名挂载目标；文件名与 realm 名不一致时 Keycloak 会拒绝启动。登录页标题取自 `KC_REALM_DISPLAY_NAME`，不填时与 realm 名相同；和 realm 文件的其他内容一样，只在首次导入时生效。浏览器 flow 使用 `shared-browser`：先检查 Keycloak SSO Cookie，再进入中性登录主题 `shared`，由共享 Authenticator 处理密码、邮箱码和短信码。Cookie 和共享 Authenticator 都是 alternative execution，让已有 Realm 会话能跳过凭证页。

- 团队用组表示，放在 `/teams` 下，例如 `/teams/platform`。
- 启用暴力破解防护，密码至少 12 位。
- 每个应用和环境独立登记 confidential OIDC client，回调 URI 精确匹配；`realm/clients/freshai-user.json` 是 FreshAI 普通用户 client 模板。应用需要专属登录文案时，新建继承 `shared` 的子主题并在 Client 的 Login theme 中选择它；子主题定义 `sharedConsentNotice` 时，登录页按 Client 的 Home URL 显示 `/terms` 与 `/privacy` 条款链接，未设置 Home URL 则不显示。FreshAI 使用 `freshai` 子主题。管理端使用独立 `freshai-admin` client，按下方通用模板在管理后台创建，去掉角色映射器；它只确认身份。FreshAI 只让已有的 active 管理员账号登录：首次登录时，已验证邮箱恰好匹配一个未绑定的管理员账号才绑定，之后按 `(iss, sub)` 登录，从不由 Keycloak 创建或启用管理员。
- 邮箱验证码和 Keycloak 注册验证依赖 SMTP。运行中的 realm 不会解析 SMTP 设置里的 `${...}`，部署后须在后台填写实际值，之后改邮箱也在后台改；发信失败时日志只记渠道和异常类型。登录页默认中文，可切换英文；手机号验证码由 Keycloak custom authenticator 调用阿里云 Dypnsapi。FreshAI 等应用容器不发送共享登录验证码。错码次数和每 IP 请求配额使用原子过期槽位；每次重发生成独立挑战键，避免并发重发时旧验证码消费新挑战。
- 新用户只通过验证码登录自动注册；Keycloak 自带注册页不收集已验证联系方式，realm 的注册流程绑定为只含 Deny Access 的 `shared-registration-disabled`。
- 首次登录只收集实际选择并验证的联系方式；部署后用 `kcadm.sh update users/profile -r <realm> -f realm/user-profile.json` 将邮箱、名字和姓氏设为可选，避免手机号新用户被默认 User Profile 要求补填邮箱。该文件同时声明短信登录写入的 `phone_number`（`+86` 加 11 位号码）和 `phone_number_verified`，仅管理员可在用户详情页查看，用户本人不可查看或修改。邮箱验证码新建的用户以邮箱为用户名，短信验证码新建的用户以 E.164 手机号（如 `+8613800000000`）为用户名，管理后台默认搜索框可按号码片段查找（`*13800*`）；用户名会以 `preferred_username` 出现在各应用的令牌中。
- 开发应用的 OIDC 登录时，用本机 Keycloak 实例测试，不往生产 realm 放开发用 client。

realm 文件只在 realm 不存在时导入，之后的改动先改文件并审查，再用 `kcadm.sh` 应用到线上，步骤见 README。

## 新应用接入

1. 在管理后台为应用的每个环境新增一个 Client，按下方模板设置。只有带自定义映射器或主题的 Client（如 `freshai-user.json`）才在仓库保存 JSON；线上 Client 以 Keycloak 数据库为准，由 `just keycloak-backup` 备份。
2. 把该应用的客户端角色授给需要访问它的团队组（组的 `clientRoles`），用户随组获得角色。
3. 应用侧配置 issuer（`https://<KC_PUBLIC_HOST>/realms/<KC_REALM>`）、Client ID 与 Client Secret；Client Secret 只放在部署环境。
4. 应用用 `(iss, sub)` 映射本地用户，不用邮箱当主键。各应用继续在本地管理用户状态和业务权限；共享 Realm 的组或角色不会自动提升 FreshAI 的管理员权限。

### Client 模板

应用内能做什么用客户端角色表示（`user`、`admin`）。Keycloak 默认只把客户端角色写进 Access Token，模板里的映射器把本应用的角色写进 ID Token 的 `resource_access.<client>.roles`，应用登录回调直接读取。客户端强制 PKCE（S256），关闭隐式流程与密码直连授权。`<app>` 与回调地址按应用替换；`secret` 留空由 Keycloak 生成，再从管理后台复制到应用的部署环境。

```json
{
  "clientId": "<app>",
  "protocol": "openid-connect",
  "publicClient": false,
  "standardFlowEnabled": true,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "redirectUris": ["https://<app 域名>/auth/oidc/callback"],
  "webOrigins": ["+"],
  "attributes": { "pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+" },
  "protocolMappers": [{
    "name": "client roles in id token",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-usermodel-client-role-mapper",
    "config": {
      "usermodel.clientRoleMapping.clientId": "<app>",
      "claim.name": "resource_access.<app>.roles",
      "jsonType.label": "String",
      "multivalued": "true",
      "id.token.claim": "true",
      "access.token.claim": "false",
      "userinfo.token.claim": "true",
      "introspection.token.claim": "true"
    }
  }]
}
```

客户端角色写在 realm 文件的 `roles.client.<app>` 下，例如 `[{"name": "user"}, {"name": "admin"}]`。

## 应用管理端接入

适用于应用已实现管理员 OIDC 登录、Keycloak 只负责确认身份的情况，以 FreshAI 管理端为例。管理员权限仍在应用本地，Client 不带角色。先在测试 realm 跑通，再在正式 realm 重做一遍。

1. **确认目标部署和域名。** 以服务器上 Traefik 路由标签为准（`docker inspect` 查看 `traefik.http.routers.*.rule`），不要只看私有清单；测试和生产管理端域名通常只差一个前缀，填错会把登录跳回另一套环境。
2. **打开管理后台。** `ssh -N -L 18180:127.0.0.1:18180 root@<server>` 后访问 `http://127.0.0.1:18180/admin/`，用 master realm 管理员登录，左上角切到目标 realm。
3. **创建 Client**（Clients → Create client）：

    | 页 | 设置 |
    |---|---|
    | General settings | OpenID Connect；Client ID 如 `freshai-admin` |
    | Capability config | Client authentication On；只勾 Standard flow；Require PKCE On，方法 `S256` |
    | Login settings | Home URL 为管理端地址；Valid redirect URIs 只填应用的回调地址原文（FreshAI 为 `https://admin.example.com/api/auth/admin/oidc/callback`），不用通配符；其余留空 |

4. **确认 scope 并取 secret。** Client scopes 页签中 `email` 为 Default；Credentials 页签复制 Client secret，只粘贴到应用的部署环境，不进对话、仓库或日志。
5. **配置应用。** FreshAI 后端设置 `ADMIN_OIDC_ENABLED=true`、`ADMIN_OIDC_ISSUER=https://auth.example.com/realms/<realm>`、`ADMIN_OIDC_CLIENT_ID`、`ADMIN_OIDC_CLIENT_SECRET`、`ADMIN_OIDC_CALLBACK_URI`，重新部署。Dokploy 环境变量里的 `$` 写成 `$$`。
6. **只读验证。**
    - `GET https://admin.example.com/api/auth/admin/oidc/status` 返回 `{"enabled":true}`。
    - `GET .../api/auth/admin/oidc/start` 302 到目标 realm，`client_id` 与 `redirect_uri` 正确。
    - 直接请求 Keycloak 授权地址：错误回调返回「无效的参数 : redirect_uri」，缺 PKCE 时回跳带 `Missing parameter: code_challenge_method`。
7. **首次登录绑定。** FreshAI 只在 ID Token 的 `email_verified=true`，且邮箱恰好匹配一个 active、未绑定的管理员时绑定 `(iss, sub)`，不建号。管理员只能由启动时的 `ADMIN_IDENTIFIER` 写入，它必须是真实邮箱。用无痕窗口打开管理端 `/sign-in`，点企业账号登录并用邮箱验证码完成；后端日志出现 `auth.oidc outcome=success portal=admin binding=first_linked` 即成功。

一个管理员账号只能绑定一个身份，FreshAI 没有解绑入口。测试 realm 用单独的测试管理员和邮箱，不要用真实管理员；切到正式 realm 时在正式 realm 重建 Client，换 issuer 与 secret，由真实管理员首次登录绑定。

| 现象 | 原因与处理 |
|---|---|
| `oidc/status` 仍为 `false` | 环境变量没进容器，或请求的是另一套部署；核对容器环境变量名和路由域名 |
| 登录后跳到另一个域名 | Client 的 Valid redirect URIs 与 `ADMIN_OIDC_CALLBACK_URI` 写成了别的环境，两处一起改 |
| 管理端 `/login` 404 | FreshAI 管理端登录页是 `/sign-in` |
| 回到登录页只显示通用失败 | 查后端日志 `auth.oidc outcome=failed reason=...`：邮箱未验证、没有或有多个匹配管理员、已绑定其他身份 |
| 收不到验证码或不自动建号 | 目标 realm 的 Realm settings → Email 未配置 SMTP，或 Login 页签未开启 User registration |

## 验证记录

本机按 `compose.yaml` 启动（Docker 网段已耗尽，测试时两个容器改挂已有网络，不改 Keycloak 配置）：

- realm 首次启动导入成功，重启后跳过导入，测试用户保留。
- 发现文档、JWKS 可访问；生产 hostname 配置下 issuer 为 `https://<KC_PUBLIC_HOST>/realms/<KC_REALM>`。
- 用按上面模板配置的临时测试客户端，经真实登录页跑通授权码 + PKCE：`/teams/platform` 成员的 ID Token 带 `admin`、`user` 角色，无组用户不带角色。该测试客户端已从 realm 文件移除。
- 负控：未登记的回调地址返回 `Invalid parameter: redirect_uri`；不带 PKCE 的请求被拒绝（`Missing parameter: code_challenge_method`）。
- 非默认 realm 名（`KC_REALM=dewell`）：导入文件按 realm 名挂载，导入成功，issuer 为 `https://<KC_PUBLIC_HOST>/realms/dewell`。
- 健康检查只认 `/health/ready` 的 200：启动期间保持 starting，变为 healthy 时发现文档已可访问。
- 管理后台经隧道打开时，master realm 默认仍用公网地址认证，而公网屏蔽了 master realm，页面报 `Timeout when waiting for 3rd party check iframe message`；把 master realm 的 `frontendUrl` 设为隧道地址后，管理后台的 `authServerUrl` 变为隧道地址，业务 realm 的 issuer 不变。
- 恢复演练：`pg_dump` 导出后恢复到新数据卷，Keycloak 在新卷上启动，原有用户与登录流程均正常。
- `KC_CACHE=local` 之前，重启后的容器会先尝试加入上一个容器登记的集群节点，约 20 秒后才放弃；改为单节点缓存后不再出现该等待。

未在本机验证：Traefik 路径过滤、Let's Encrypt 证书与 Dokploy 部署，需在目标服务器上线时确认。

## 已有 Realm 的共享登录升级

在 Docker 主机的仓库目录运行 `python3 scripts/keycloak/upgrade_shared_login.py
--container <容器> --realm <Realm> --snapshot <私密快照路径>` 默认只做检查和预览。
显式加 `--apply` 后创建/校验共享流程、绑定主题并开启自注册；`--rollback --apply`
按原快照恢复。命令要求先部署自定义镜像并使用容器内 Admin CLI 登录。
完整登录、执行及恢复命令见仓库 `deploy/keycloak/README.md` 的
“Upgrade an existing realm with one configuration command”。此命令不负责镜像发布和短信/邮件凭据配置。
