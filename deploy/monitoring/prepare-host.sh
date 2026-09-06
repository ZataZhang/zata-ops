#!/usr/bin/env bash

# 上传配置后在中心服务器运行；仅首次生成凭据，不启动容器。
set -euo pipefail
monitoring_domain="${1:?缺少根域名}"
[[ "$monitoring_domain" =~ ^[A-Za-z0-9.-]+$ ]]
monitoring_dir=/opt/apps/monitoring
test -f "$monitoring_dir/docker-compose.yml"
test -d /opt/traefik/dynamic
umask 077
cd "$monitoring_dir"

if [[ ! -f .env ]]; then
  grafana_password="$(openssl rand -hex 24)"
  printf 'DOMAIN=%s\nNODE_NAME=monitoring\nGF_SECURITY_ADMIN_PASSWORD=%s\n' \
    "$monitoring_domain" "$grafana_password" > .env
fi
if [[ ! -f collector.env ]]; then
  ingest_password="$(openssl rand -hex 24)"
  printf 'NODE_NAME=app-server-1\nLOKI_ENDPOINT=https://grafana.%s\nMETRICS_ENDPOINT=https://grafana.%s/api/v1/write\nINGEST_USER=app-server-1\nINGEST_PASSWORD=%s\n' \
    "$monitoring_domain" "$monitoring_domain" "$ingest_password" > collector.env
fi
# 此文件仅由本脚本生成并由 root 持有，不从不可信上传内容加载。
source collector.env
password_hash="$(printf '%s\n' "$INGEST_PASSWORD" | openssl passwd -apr1 -stdin)"
printf '%s:%s\n' "$INGEST_USER" "$password_hash" > /opt/traefik/dynamic/monitoring-users
cat > /opt/traefik/dynamic/monitoring-ingest.yml <<'YAML'
http:
  middlewares:
    monitoring-ingest:
      basicAuth:
        usersFile: /etc/traefik/dynamic/monitoring-users
        removeHeader: true
YAML
chmod 600 .env collector.env /opt/traefik/dynamic/monitoring-users /opt/traefik/dynamic/monitoring-ingest.yml
docker compose config --quiet
printf '配置已准备；凭据保存在受保护的 .env 和 collector.env，未输出到日志。\n'
