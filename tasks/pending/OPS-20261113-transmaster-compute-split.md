# TransMaster 迁移：计算与服务端分离（出口与入站保留在入口节点）

状态：已执行（2026-11-13 完成切换，待补 CD 变量与发布脚本修正）
说明：本页是公开仓库内的任务记录，**不写真实主机地址、域名、网段、仓库地址与规格**。
真实值统一查 `config/private/`（已 gitignore）与 `docs/guides/private-inventory.md`。

占位符约定：

| 占位符 | 含义 |
|---|---|
| `<ENTRY_NODE>` | 现有入口节点：Traefik 与公网出口所在主机 |
| `<COMPUTE_NODE>` | 目标计算节点：应用容器运行主机 |
| `<APP_DOMAIN>` | 应用公网域名 |
| `<REGISTRY>` | 私有镜像仓库 |
| `<ENTRY_PRIV>` / `<COMPUTE_PRIV>` | 两台主机的私网地址 |
| `<ENTRY_GW>` / `<COMPUTE_GW>` | 两台主机各自的 VPC 网关 |

## 0. 结论先行

目标拓扑：算力在 `<COMPUTE_NODE>`，公网入站经 `<ENTRY_NODE>`，出站源 IP 仍是入口节点。

勘察推翻了"在计算节点上加一条默认路由指向入口节点"的设想：

- 两台主机**不在同一子网，也不共享二层**，各自的路由都把对端标记为 `via <本侧 VPC 网关>`。
  两侧各有一条 `/32 ... onlink` 的对端路由，本质是经网关转发的 L3 路由。
- VPC 对等连接是**点到点**的：只保证"目的地址是对端私网 IP"的包能通过，不支持把对端主机
  当作其它目的地址的三层下一跳，也不传递路由。
- 因此 `default via <ENTRY_PRIV> onlink` 只会把 ARP 下一跳换成 VPC 网关，网关按目的 IP
  直接把包送出公网，包不会进入入口节点。

结论：出口 NAT 必须走**隧道**（本次用 WireGuard，内核模块两侧自带），在计算节点上只对
该应用的容器出向流量做策略路由，不动整机默认路由，避免影响同机其它业务。

## 1. 勘察要点（值见私有清单）

- 入口节点：Swarm 单节点 manager，Traefik 用 swarm provider，应用端口不发布到宿主；
  ufw 未启用；FORWARD 默认 DROP 且带 Docker 链。
- 计算节点：Swarm 未初始化，已有独立 compose 业务与本机 Traefik；ufw 启用且 `deny routed`；
  已存在同名本地 bridge，与入口节点的 overlay 同名（见第 3 节）。
- 依赖（数据库/缓存/对象存储/配置中心）全部走**公网端点**，现有白名单按入口节点的出口 IP 配置。
- 应用四个服务：backend、frontend、celery-worker、celery-beat；前三者需要入口网络，
  后两者只需要应用内网。
- 发布工具在业务仓库，自带"排空 → 部署 → 校验 → 切换指针 → 失败回滚"流程，但假定应用
  容器运行在 manager 本机。

## 2. 目标拓扑

```
公网用户
   │ 443（DNS: <APP_DOMAIN> → <ENTRY_NODE>）
   ▼
[ENTRY_NODE] Traefik（唯一公网入口，证书在此）
   │ swarm overlay
   ▼
[COMPUTE_NODE] backend / frontend / celery-worker / celery-beat
   │ 容器出向
   ▼
策略路由（仅该应用容器）→ wg0 ──隧道（走私网）──▶ [ENTRY_NODE] wg0 → MASQUERADE → 公网
```

保持不变：域名、证书、配置中心只读账号、远程数据库/缓存/对象存储、镜像仓库、
GitHub CD 入口。

## 3. 调度与入站：两个方案

### 方案 b（采用）：计算节点加入入口节点的 Swarm 作为 worker

- 复用现有发布链路与 Traefik swarm provider，通过 overlay 直达 worker 上的任务，
  **不发布宿主端口、不改 Traefik 路由规则**。
- 迁移是同栈就地滚动（`stop-first`，逐服务），Beat 始终单实例。
- 代价：计算节点会新增 ingress 网络与 Docker iptables 链；需要 VXLAN/控制面端口。

### 方案 a（未采用）：计算节点独立部署 + 入口节点反代

- 更解耦，但需要把业务仓库 CD 从 swarm 改成 compose、入口节点 Traefik 增加 file provider
  （重建会短暂中断 80/443）、在计算节点发布应用端口。
- 结论：不在同一次变更里同时做"迁移 + 换发布范式"，列为后续演进。

## 4. 执行阶段（已完成）

### P0 资料与预检

1. `just private-backup`；记录当前 release、现役 stack 摘要、发布锁与 Beat 归属。
2. 核对业务仓库模板与服务器模板的差异（服务数量、约束变量、网络名）。
3. 记录计算节点上既有业务的容器与网络快照，作为回退基线。
4. 证据写入 `config/private/` 下的应用目录，不写凭据值。

### P1 计算节点准备

- 预建 `docker_gwbridge`（换用未被占用的网段，避开 `docker0` 的默认段）。
- ufw 仅放行入口节点私网：`2377/tcp`、`7946`、`4789/udp`、`51820/udp`，并放行 `wg0` 入向。

### P2 出口隧道与 NAT

- 两端 `/etc/wireguard/wg0.conf`：入口节点监听，计算节点指定 endpoint 与 keepalive。
- 计算节点侧：`AllowedIPs = 0.0.0.0/0` + `Table = off`（否则 WireGuard 的加密路由会丢弃
  非隧道网段的目的地址，且 wg-quick 会改默认路由）；策略路由只用 `fwmark` 命中容器出向。
- 入口节点侧：`ip_forward` + `MASQUERADE` + FORWARD 放行 + MSS clamp。
- 计算节点侧：`PREROUTING -i <gwbridge> -j MARK`、`ip rule fwmark`、独立路由表默认走 wg0、
  MSS clamp、`ufw route allow` 双向。

### P3 调度

- 计算节点以 worker 身份加入；对齐发布模板的 placement 约束与网络名。
- 约束用**节点标签**而非角色：换计算节点时先给新机打标签、发布，再摘旧标签并 drain，CI 与模板都不用改。
- 确认 worker 能从私有仓库拉取镜像（`--with-registry-auth`）。

### P4 切换

预演（dry-run）→ 排空消费（不 purge）→ 部署 → 等待健康 → 切换发布指针 → 恢复消费。

### P5 验证

- 公网：首页、`/health`、`/docs`、`/openapi.json`、静态资源、CORS 预检、旧域名跳转。
- 容器内：出口 IP 等于入口节点公网 IP；数据库/缓存/配置中心/对象存储可连。
- 后台：worker 消费正常、`celery inspect ping`、Beat 单实例。
- 回归：计算节点上原有业务域名仍正常。

### P6 回滚

先排空新 worker 已领取任务，再用迁移前的 stack 文件重新部署，placement 回到 manager；
确认入口恢复、Beat 单实例后再停隧道。

## 5. 持久化

- 两端 `systemctl enable --now wg-quick@wg0`，NAT / 策略路由 / MSS 写在 PostUp/PostDown。
- 计算节点的 netplan 由 cloud-init 托管，不把自定义规则塞进 netplan。
- 两端私网均为 DHCP 动态：重启后需复核地址，隧道 endpoint、安全组与发布目标都依赖当前值。

## 6. 云侧变更（控制台）

- 入口节点安全组：放行计算节点私网 → `51820/udp`、`2377/tcp`、`7946`、`4789/udp`。
- 计算节点安全组：放行入口节点私网 → `7946`、`4789/udp`。

## 7. 实际执行中踩到的坑

1. **同名网络冲突**：计算节点上已有与入口节点 overlay 同名的本地 bridge（不同网段）。
   worker 上的任务被拒：`invalid config for network <name>: no configured subnet contains IP address ...`。
   处理：新建独立命名的 overlay，入口节点 Traefik 同时接入新旧两个网络，应用栈改用新网络。
2. **发布脚本假定应用在本机**：脚本用本机 `docker ps` / `docker inspect <container_id>`
   校验容器健康，任务落到 worker 后必然失败；其失败回滚路径也会重复同样的调用而中止。
   表现：首次切换部分生效（入口型服务按 `failure_action` 回到 manager，后台服务留在 worker，
   未出现双 Beat），最终改为手工 `docker stack deploy` 完成并逐项验证。
3. WireGuard 若沿用 `AllowedIPs = <peer>/32`，加密路由只接受该目的地址，转发流量会被丢弃；
   必须配 `AllowedIPs = 0.0.0.0/0` + `Table = off`。

## 8. 验收清单

- [x] P0 证据与备份完成
- [x] P1 gwbridge / ufw 就绪，既有业务未受影响
- [x] P2 隧道双向可达，容器出口 = 入口节点公网 IP，主机出口不变
- [x] P3 计算节点以 worker 身份 Ready，镜像可拉取
- [x] P4 预演通过后完成切换，Beat 单实例
- [x] P5 公网 / 依赖 / 后台任务 / 回归全部通过
- [ ] P6 回滚：已实际走通一次"部分回滚"，但发布脚本在应用位于 worker 时无法排空，需先修脚本
- [x] 证据归档到 `config/private/`，私有清单与部署记录同步

## 9. 待办

- [x] 调度约束改为节点标签 `node.labels.app.transmaster==true`（服务器模板、当前 release、业务仓库默认值已同步）
- [x] 修 `deploy_nacos.py`：改为集群任务状态 + 一次性容器执行 Celery 控制（业务仓库改动，待提交后随发布包生效）
- [ ] 观察计算节点内存 3 天
