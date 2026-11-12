# Swarm 生产部署：入口与计算分离

本页记录把「公网入口」和「应用算力」拆到不同主机后，在 Docker Swarm 上落地的
结构、必须遵守的约束和实际踩过的坑。命令里的 `<...>` 是占位符，真实值见
[私有清单](private-inventory.md)；发布流程本身见
[自动部署迁移与任务排空](deployment-migration.md)。

## 适用场景

- 一台主机负责公网入口、TLS 与出口地址，另一台（或多台）负责跑应用容器；
- 需要在不改 CI、不改镜像构建的前提下更换算力节点；
- 应用依赖远程数据库 / 缓存 / 对象存储，且对端按出口 IP 做白名单。

## 一、拓扑与角色

```
公网 → [ENTRY] Traefik（80/443、证书、唯一入口）
             │ Swarm overlay
             ▼
       [COMPUTE] 应用服务（worker 节点）
             │ 容器出向
             ▼
       策略路由 → 隧道 ──▶ [ENTRY] → MASQUERADE → 公网
```

- **ENTRY**：Swarm manager + Traefik。它同时是公网出口，所以所有依赖方看到的源 IP 是它。
- **COMPUTE**：Swarm worker。只跑应用，不发布宿主端口，入口流量走 overlay 直达。

容器不发布宿主端口是一条硬约束：一旦发布，端口会绕过宿主防火墙规则，
把“只给入口节点访问”变成“全网可达”。

## 二、网络：三类必踩的坑

### 1. 私网“互通”不等于“能当三层下一跳”

两台主机能互相 ping，只说明**目的地是对端 IP** 的包能过去。要让“目的地是公网”的包
经由对端送出，对端必须能作为路由的下一跳。

判据：看路由表里对端的下一跳是什么。

```bash
ip route get <PEER_PRIVATE_IP>
ip route get 1.1.1.1
```

如果两条都显示 `via <本侧 VPC 网关>`，说明两台机器并不共享二层，中间是路由器。
常见误解是加一条 `default via <PEER_PRIVATE_IP> onlink`：`onlink` 只是让内核把包发往
网关的 MAC，网关仍按**目的 IP** 选路，包不会进入对端主机。

对等连接（peering）是点到点的，不支持传递路由，也不允许把对端主机当其它目的地址的
下一跳。结论：**出口 NAT 必须走隧道**，或者把两台主机放进同一子网。

### 2. overlay 与本地 bridge 同名

worker 上如果已经有一个与集群 overlay **同名**的本地 bridge（例如之前自己跑 Traefik
时创建的），任务会在该节点上被拒，报错形如：

```
invalid config for network <name>: invalid endpoint settings:
no configured subnet contains IP address <overlay-ip>
```

原因不是端口或权限，而是同名网络在节点上解析到了本地 bridge 的另一个网段。

两种处理：

1. **新建一个专用 overlay 名**（推荐，影响面最小）：入口节点的 Traefik 同时接入新旧
   两个网络，应用栈改用新名字，并在服务标签里指定
   `traefik.docker.network=<new-overlay>`；
2. 迁走本地 bridge：断开现有容器、删除网络、用新名字重建，并同步所有引用它的 compose
   文件（这会动到同机其它业务，风险更高）。

检查命令：

```bash
docker network ls | grep <overlay-name>     # 看 driver 是 overlay 还是 bridge
ip -4 addr show | grep <overlay-subnet>
```

### 3. `docker_gwbridge` 与 `docker0` 网段冲突

Docker 默认给 `docker_gwbridge` 用 `172.18.0.0/16`。如果节点的 `docker0` 已经占了这段
（很常见），加入 Swarm 时创建网关桥会失败。加入前先手工建：

```bash
docker network create -d bridge --subnet 172.21.0.0/16 --gateway 172.21.0.1 \
  --opt com.docker.network.bridge.name=docker_gwbridge \
  --opt com.docker.network.bridge.enable_icc=false \
  --opt com.docker.network.bridge.enable_ip_masquerade=true docker_gwbridge
```

## 三、出口 NAT：隧道 + 策略路由

### WireGuard 的两个必须项

```
[Interface]
Address = 10.99.0.2/32
MTU = 1420
Table = off            # 不要让它改主路由表
PostUp = ...
[Peer]
PublicKey = <PEER_PUBKEY>
AllowedIPs = 0.0.0.0/0  # 见下，不能只写对端 /32
Endpoint = <ENTRY_PRIVATE_IP>:51820
PersistentKeepalive = 25
```

- **`AllowedIPs` 是加密路由的出口白名单**：只写对端 `/32` 时，去公网的转发包目的地址
  不在白名单里，会被内核直接丢弃，表现为“隧道通了但只有对端 IP 能通”。
- **`Table = off`**：否则 wg-quick 会往主路由表塞默认路由，把整机流量都带走。我们只想
  让特定容器的流量进隧道。

### 策略路由用标记，不要按网段硬编码

按入口网卡打标记，比写死容器网段稳：overlay 网段会随栈重建变化，而入口网卡名是稳定的。

```bash
iptables -t mangle -A PREROUTING -i docker_gwbridge -j MARK --set-mark 0x64
ip rule add fwmark 0x64 lookup 100
ip route add default dev wg0 table 100
iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE
```

入口节点侧：

```bash
iptables -A FORWARD -i wg0 -o eth0 -j ACCEPT
iptables -A FORWARD -i eth0 -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -A POSTROUTING -s 10.99.0.0/24 -o eth0 -j MASQUERADE
```

注意：manager 的 `FORWARD` 策略常常是 `DROP`（Docker 自己的链只放行容器流量），
必须显式放行隧道方向，否则包进来了也出不去。

### MTU 与 MSS

隧道有额外头部开销，把 `wg0` 设成 1420 并在两侧对 SYN 做 MSS 钳制，否则大响应会卡住：

```bash
iptables -t mangle -A FORWARD -o wg0 -p tcp --tcp-flags SYN,RST SYN \
  -j TCPMSS --clamp-mss-to-pmtu
```

### 验证要分两层

```bash
# 容器内：出口必须是入口节点的公网 IP
docker exec <container> curl -s https://ifconfig.me

# 主机内：出口必须仍是自己，证明整机默认路由没被改
curl -s https://ifconfig.me
```

只验第一层会掩盖“整机流量被劫持”的问题；只验第二层会掩盖“容器没走隧道”。

## 四、调度：用节点标签，不要用角色

```yaml
placement:
  constraints:
    - "node.labels.app.transmaster==true"
```

换成标签后，换算力节点是纯运维动作，CI 与模板都不用改：

```bash
# 新机
docker swarm join --token <worker-token> <manager>:2377
docker node update --label-add app.transmaster=true <new-node>
# 发布或 docker service update --force 让任务落到新节点
# 旧机
docker node update --label-rm app.transmaster <old-node>
docker node update --availability drain <old-node>
```

顺序很重要：**先给新节点打标签、等任务迁过去，再摘旧标签并 drain**。
任何时刻都要有节点满足约束，否则任务会变成 Pending。

用 `node.role==worker` 的坏处：多台 worker 时新旧同时满足约束，任务会乱落；
少一台时又没有节点满足约束。

## 五、发布脚本不要假设“应用在本机”

manager 能操作集群对象（service、task、node），但 **容器是节点本地对象**：

| 操作 | manager 可见？ |
|---|---|
| `docker service ps / inspect` | 是（集群对象） |
| `docker inspect <task-id>` | 是（任务状态含 ContainerID） |
| `docker inspect <container-id>` | **否** |
| `docker ps` | 只列本机容器 |
| `docker exec` | 只能进本机容器 |

因此健康判定与任务控制要改用：

1. 任务状态：`docker service ps --filter desired-state=running` +
   `docker inspect <task>` 的 `Status.State` / `ContainerStatus.ContainerID`；
2. 稳定性：同一个容器 ID 连续两轮仍在运行，才算排除重启循环；
3. 功能性：在 manager 上用**同一镜像 + 同一份环境变量**起一次性容器执行检查
   （例如 Celery 控制脚本），`--network host` 即可，用完即删；
4. 端到端：公网健康入口返回 200。

容器级 healthcheck 的细节在远程节点上读不到，别把它当唯一判据。

提交栈之前还要校验**调度是否可满足**：`docker stack deploy` 在没有任何节点满足约束时
同样返回成功，任务随后全部 Pending，"发布成功"是假的。预检应遍历节点，按
`Ready` + `Active` 过滤，逐条比对 `node.role` / `node.hostname` / `node.id` /
`node.labels.<key>`，并要求匹配节点数不少于副本数；无法识别的约束语法应放行而不是阻断。

## 六、防火墙是多层的

| 层 | 影响 |
|---|---|
| 云安全组 | 跨主机、跨 VPC 的准入，控制台/API 才能改 |
| 宿主 ufw | `deny routed` 会拦容器转发；用 `ufw route allow` 放行成对接口 |
| Docker 链（DOCKER-USER / DOCKER-FORWARD） | 发布端口会绕过 ufw 的入向规则 |
| 应用层中间件/鉴权 | 入口节点上的 header 注入、请求体上限等 |

经验：

- 发布宿主端口就等于对该接口全网开放，宿主防火墙管不到；
- 需要“只给入口节点访问”时，优先不发布端口，走 overlay；
- `ufw` 的 route 规则要双向写（进和出），否则只有单方向能通；
- 云安全组是最外层，宿主上看到监听正常但连不通时，先查安全组。

## 七、换节点前必须清点的状态

换算力节点：

- [ ] bind 挂载的运行时目录（业务配置、上传、凭据文件），复制时保留属主 UID
- [ ] 命名卷（日志、调度状态）；调度状态丢失通常可接受，但要确认不会导致重复调度
- [ ] 节点标签
- [ ] 镜像仓库登录（`--with-registry-auth` 会传递，但节点要能出网拉取）

换入口节点：

- [ ] DNS 记录与 TTL（提前降到 60s）
- [ ] TLS 证书（ACME 卷，或让新机重新签发）
- [ ] Swarm manager 身份（`promote`/`demote`）
- [ ] 出口隧道的对端配置与 `MASQUERADE`
- [ ] 到计算节点的路由
- [ ] 安全组里对计算节点私网的放行
- [ ] 发布用的 SSH 目标与 `known_hosts`

## 八、切换与回滚

- **调度器只能有一个**：定时任务（如 Celery Beat）在两个地方同时跑会重复触发。
  切换时先把旧的缩到 0，再起新的；回滚时顺序相反。
- **排空不是停机宽限**：暂停消费、等 active/reserved/scheduled 清空、再更新服务，
  更新后恢复消费；队列不能 purge。宽限期只是退出缓冲。
- **发布指针要唯一**：CD 与手工发布若各有一套 `current`，回滚时容易看错版本。
  统一成一个根目录、一个指针。
- 回滚同样要排空新实例已领取的任务，再切回旧版本。

## 九、故障速查

| 症状 | 先查什么 |
|---|---|
| 任务在 worker 上 Pending | 节点标签 / 约束是否满足；节点是否 Ready |
| `no configured subnet contains IP` | 同名 bridge 与 overlay 冲突 |
| 容器出不去公网，但主机可以 | 标记规则、`ip rule`、`Table=off`、`AllowedIPs` |
| 出了公网但源 IP 不对 | 入口节点 `MASQUERADE` 与 `ip_forward` |
| 小文件正常、大响应卡住 | MSS 钳制与隧道 MTU |
| 宿主能连、容器不能连数据库 | 安全组按容器方向/子网放行，或隧道未生效 |
| 公网 502，但服务 healthy | 入口节点与任务不在同一 overlay，或 `traefik.docker.network` 指向错误 |
| 发布脚本在 worker 上失败 | 是否用了 `docker ps` / `docker inspect <container>` / `docker exec` |
| 定时任务重复执行 | 两个 Beat 同时在跑 |

## 十、验收清单

- [ ] 公网首页 / 健康 / API 文档 / 静态资源 / CORS 全部 200
- [ ] 容器内出口 = 入口节点公网 IP；主机出口仍为自己
- [ ] 数据库、缓存、配置中心、对象存储可从容器连通
- [ ] 后台任务能消费，消费队列已恢复
- [ ] 调度器单实例
- [ ] 同机其它业务回归正常
- [ ] 发布 dry-run 通过，且没有切换指针
- [ ] 回滚路径已确认（含排空）
