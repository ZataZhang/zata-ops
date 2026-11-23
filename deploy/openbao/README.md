# OpenBao 单节点部署

本目录包含固定版本的 OpenBao Compose 配置、Raft 与审计配置、TLS 准备脚本，以及供运维人员使用的权限策略。

部署、初始化、连接和备份步骤见 [OpenBao 部署与维护](../../docs/guides/openbao.md)。
个人账号、权限、登录、续期和密钥管理见 [OpenBao 日常使用指南](../../docs/guides/openbao-usage.md)。
为 Dokploy 配置受限的 HTTPS API 入口时，使用 [Traefik 配置](../../docs/guides/openbao-traefik.md)。
Traefik 路由模板由 `traefik/render_dynamic.py` 根据忽略跟踪的实例 `ingress.env` 生成；来源白名单支持 Dokploy 和任意数量的团队出口 CIDR。
