# Monitoring Stack — Vector + Loki + Prometheus + Grafana

独立部署的可观测性栈，用于采集容器日志和主机指标。生产准备、凭据管理、跨主机接入及验证边界见 `docs/architecture/monitoring-stack.md` 的“独立日志与指标服务器”。不要使用旧的 `env provision --with-monitoring` 默认密码初始化路径。

## 快速启动

```bash
cd deploy/monitoring
cp .env.example .env
# 编辑 .env，填写 DOMAIN 和随机生成的强密码，并先配置 Traefik 写入认证中间件
vim .env
docker compose up -d
```

## 包含组件

- **Vector**：采集 Docker 容器 stdout 日志，解析 JSON，推送到 Loki。
- **Loki**：日志存储与查询。
- **Prometheus**：接收 Vector 推送的主机及采集器指标，应用 `/metrics` 需另配。
- **Grafana**：统一面板，自动 provisioning Prometheus + Loki 数据源。

## 访问

- Grafana：`https://grafana.${DOMAIN}`
- Prometheus：`http://prometheus:9090`（容器网络内，不发布宿主机端口）
- Loki：`http://loki:3100`（仅在 monitoring 网络内）

## 与下游应用对接

每台应用服务器部署一个 Vector，详见 `collector.compose.yml` 和 `collector.env.example`。应用日志输出到 stdout/stderr 即可，JSON 可提供更多结构化字段；Compose 自动提供项目和服务标签。Grafana 中通过 `node`、`compose_project`、`compose_service` 筛选。接入日志不要求应用提供 `/metrics`，请求级指标另行配置。
