#!/usr/bin/env bash
# 初始化本机开发 Keycloak：SMTP 指向 Mailpit、应用 User Profile、注册绑定操作、建本地应用 client。
# 可重复执行；已存在的 client 会更新回调地址。用法：local_setup.sh [应用地址，默认 http://localhost:3000]
set -euo pipefail

APP_URL="${1:-http://localhost:3000}"
APP_URL="${APP_URL%/}"
REALM="dewell-local"
CLIENT_SECRET="freshai-local-dev-secret"
KC_PORT=18280
COMPOSE=(docker compose -f local/docker-compose.testing.yml)
kc() { "${COMPOSE[@]}" exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"; }

echo "Waiting for Keycloak on 127.0.0.1:${KC_PORT} ..."
curl -sf -o /dev/null --retry 90 --retry-delay 2 --retry-all-errors --max-time 5 \
  "http://127.0.0.1:${KC_PORT}/realms/${REALM}/.well-known/openid-configuration"

kc config credentials --server http://127.0.0.1:8080 --realm master --user admin --password admin >/dev/null
echo '{"smtpServer":{"host":"mailpit","port":"1025","from":"noreply@example.com","fromDisplayName":"Local Keycloak","auth":"false","ssl":"false","starttls":"false"}}' \
  | kc update "realms/${REALM}" -f -
kc update users/profile -r "${REALM}" -f /opt/keycloak/data/user-profile.json
for action in shared-bind-email shared-bind-phone; do
  if ! kc get "authentication/required-actions/${action}" -r "${REALM}" >/dev/null 2>&1; then
    kc create authentication/register-required-action -r "${REALM}" -s providerId="${action}" -s name="${action}"
  fi
done

client_json=$(python3 - "$APP_URL" "$CLIENT_SECRET" <<'PY'
import json, sys
from pathlib import Path
app_url, secret = sys.argv[1], sys.argv[2]
client = json.loads(Path("deploy/keycloak/realm/clients/freshai-user.json").read_text(encoding="utf-8"))
client.update(baseUrl=app_url + "/", redirectUris=[app_url + "/api/auth/user/oidc/callback"], secret=secret)
print(json.dumps(client))
PY
)
client_id=$(kc get clients -r "${REALM}" -q clientId=freshai --fields id | python3 -c "import sys,json; c=json.load(sys.stdin); print(c[0]['id'] if c else '')")
if [ -n "$client_id" ]; then
  echo "$client_json" | kc update "clients/${client_id}" -r "${REALM}" -f -
else
  echo "$client_json" | kc create clients -r "${REALM}" -f - >/dev/null
fi

cat <<INFO
Local Keycloak is ready.
  Admin console : http://127.0.0.1:${KC_PORT}/admin/  (admin / admin)
  Mail inbox    : http://127.0.0.1:8025/
  FreshAI .env  :
    USER_OIDC_ENABLED=true
    USER_OIDC_ISSUER=http://127.0.0.1:${KC_PORT}/realms/${REALM}
    USER_OIDC_CLIENT_ID=freshai
    USER_OIDC_CLIENT_SECRET=${CLIENT_SECRET}
    USER_OIDC_CALLBACK_URI=${APP_URL}/api/auth/user/oidc/callback
INFO
