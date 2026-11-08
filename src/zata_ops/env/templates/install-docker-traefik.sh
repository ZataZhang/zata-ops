#!/usr/bin/env bash
set -Eeuo pipefail

# Ubuntu/Debian server installer for Docker Engine + Traefik.
#
# Usage:
#   bash install-docker-traefik.sh                                  # default placeholder email
#   ACME_EMAIL=you@your-domain.com bash install-docker-traefik.sh   # real mailbox
#   ACME_EMAIL= bash install-docker-traefik.sh                      # no ACME, HTTP only
#
# ACME_EMAIL falls back to example@zata-ops.com when the variable is unset.
# That address only fills the ACME account contact field: Let's Encrypt does
# not verify it and stopped sending expiration reminders in 2025, so it does
# not affect issuance. Set ACME_EMAIL to an empty string to skip the
# certificatesResolvers block entirely (HTTP only).
#
# Optional environment variables:
#   TRAEFIK_DIR=/opt/traefik
#   TRAEFIK_NETWORK=traefik
#   TRAEFIK_IMAGE=traefik:v3.7
#   ACME_EMAIL=you@your-domain.com
#   ENABLE_HTTPS_REDIRECT=true|false
#   INSTALL_SAMPLE=true|false
#   WHOAMI_HOST=whoami.example.com

TRAEFIK_DIR="${TRAEFIK_DIR:-/opt/traefik}"
TRAEFIK_NETWORK="${TRAEFIK_NETWORK:-traefik}"
TRAEFIK_IMAGE="${TRAEFIK_IMAGE:-traefik:v3.7}"
# Note the "-" (not ":-"): unset falls back to the default placeholder, while
# an explicit empty value (ACME_EMAIL=) still disables ACME. When ACME is
# disabled the script writes no certificatesResolvers block, so Traefik only
# serves the built-in self-signed cert, which is fine for local dev.
#
# Do NOT pass a reserved placeholder such as "admin@example.com" /
# "you@example.com": Let's Encrypt rejects those registrations with
# invalidEmail ("Contact emails @example.com are forbidden"), acme.json is
# never created, and Traefik serves the TRAEFIK DEFAULT CERT to every
# visitor while the installer still reports success. If that already
# happened, repair the running install with the "fix-acme-email.sh"
# companion script in this repo.
DEFAULT_ACME_EMAIL="example@zata-ops.com"
ACME_EMAIL="${ACME_EMAIL-$DEFAULT_ACME_EMAIL}"
ENABLE_HTTPS_REDIRECT="${ENABLE_HTTPS_REDIRECT:-}"
INSTALL_SAMPLE="${INSTALL_SAMPLE:-false}"
WHOAMI_HOST="${WHOAMI_HOST:-whoami.localhost}"

SUDO=()

log() {
  printf '\n[%s] %s\n' "$(date +'%H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

require_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=()
    return
  fi

  command -v sudo >/dev/null 2>&1 || die "请用 root 运行，或先安装 sudo。"
  sudo -v
  SUDO=(sudo)
}

detect_distro() {
  command -v apt-get >/dev/null 2>&1 || die "此脚本只支持 Ubuntu/Debian 的 apt 系统。"
  [[ -r /etc/os-release ]] || die "找不到 /etc/os-release，无法识别系统。"

  # shellcheck disable=SC1091
  . /etc/os-release

  DOCKER_DISTRO="${ID:-}"
  DOCKER_CODENAME="${VERSION_CODENAME:-}"

  if [[ "$DOCKER_DISTRO" == "ubuntu" ]]; then
    DOCKER_CODENAME="${UBUNTU_CODENAME:-$DOCKER_CODENAME}"
  elif [[ "$DOCKER_DISTRO" == "debian" ]]; then
    :
  elif [[ "${ID_LIKE:-}" == *ubuntu* && -n "${UBUNTU_CODENAME:-}" ]]; then
    DOCKER_DISTRO="ubuntu"
    DOCKER_CODENAME="$UBUNTU_CODENAME"
  elif [[ "${ID_LIKE:-}" == *debian* ]]; then
    DOCKER_DISTRO="debian"
  else
    die "当前系统不是官方支持的 Ubuntu/Debian：ID=${ID:-unknown} ID_LIKE=${ID_LIKE:-unknown}"
  fi

  [[ -n "$DOCKER_CODENAME" ]] || die "无法识别发行版 codename，请手动设置 Docker apt 源。"
}

remove_conflicting_docker_packages() {
  local packages=(
    docker.io
    docker-doc
    docker-compose
    docker-compose-v2
    podman-docker
    containerd
    runc
  )
  local installed=()

  for package in "${packages[@]}"; do
    if dpkg -s "$package" >/dev/null 2>&1; then
      installed+=("$package")
    fi
  done

  if ((${#installed[@]} > 0)); then
    log "移除可能冲突的旧 Docker 发行版包：${installed[*]}"
    "${SUDO[@]}" apt-get remove -y "${installed[@]}"
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker 和 Compose 插件已存在，跳过 Docker 安装。"
    return
  fi

  detect_distro
  remove_conflicting_docker_packages

  log "安装 Docker apt 源依赖。"
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y ca-certificates curl

  log "添加 Docker 官方 apt 仓库：${DOCKER_DISTRO} ${DOCKER_CODENAME}"
  "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
  "${SUDO[@]}" curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" -o /etc/apt/keyrings/docker.asc
  "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc

  local arch
  arch="$(dpkg --print-architecture)"

  "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/${DOCKER_DISTRO}
Suites: ${DOCKER_CODENAME}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc
EOF

  log "安装 Docker Engine、Buildx 和 Compose 插件。"
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  "${SUDO[@]}" systemctl enable --now docker

  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    "${SUDO[@]}" usermod -aG docker "$SUDO_USER"
    log "已把用户 ${SUDO_USER} 加入 docker 组；重新登录后可不加 sudo 使用 docker。"
  fi
}

configure_docker_firewall() {
  log "限制 Docker 发布端口并阻止非 root 访问实例元数据。"
  "${SUDO[@]}" tee /usr/local/sbin/zata-docker-user-firewall >/dev/null <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

ensure_rule() {
  local firewall_command="$1"
  local chain_name="$2"
  shift 2
  if ! "$firewall_command" -w 10 -C "$chain_name" "$@" 2>/dev/null; then
    "$firewall_command" -w 10 -A "$chain_name" "$@"
  fi
}

remove_owned_rules() {
  local firewall_command="$1"
  local rule_number
  while true; do
    rule_number="$("$firewall_command" -w 10 -L DOCKER-USER --line-numbers -n 2>/dev/null | awk '$2 == "ZATA-DOCKER-INGRESS" {print $1; exit}')"
    [[ -n "$rule_number" ]] || break
    "$firewall_command" -w 10 -D DOCKER-USER "$rule_number"
  done
  if "$firewall_command" -w 10 -C DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null; then
    "$firewall_command" -w 10 -D DOCKER-USER -d 169.254.169.254/32 -j DROP
  fi
  if "$firewall_command" -w 10 -S ZATA-DOCKER-INGRESS >/dev/null 2>&1; then
    "$firewall_command" -w 10 -F ZATA-DOCKER-INGRESS
    "$firewall_command" -w 10 -X ZATA-DOCKER-INGRESS
  fi
}

if [[ "${1:-apply}" == "--remove" ]]; then
  remove_owned_rules iptables
  remove_owned_rules ip6tables
  if iptables -w 10 -C OUTPUT -d 169.254.169.254/32 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null; then
    iptables -w 10 -D OUTPUT -d 169.254.169.254/32 -m owner ! --uid-owner 0 -j REJECT
  fi
  exit 0
fi
if [[ "${1:-apply}" != "apply" ]]; then
  echo "Usage: zata-docker-user-firewall [apply|--remove]" >&2
  exit 2
fi

external_interface="$(ip -o -4 route get 1.1.1.1 | awk '{for (field_index = 1; field_index <= NF; field_index++) if ($field_index == "dev") {print $(field_index + 1); exit}}')"
[[ -n "$external_interface" ]] || { echo "Cannot determine external interface." >&2; exit 1; }

for firewall_command in iptables ip6tables; do
  "$firewall_command" -w 10 -S DOCKER-USER >/dev/null
  if ! "$firewall_command" -w 10 -S ZATA-DOCKER-INGRESS >/dev/null 2>&1; then
    "$firewall_command" -w 10 -N ZATA-DOCKER-INGRESS
  fi
  ensure_rule "$firewall_command" ZATA-DOCKER-INGRESS \
    -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ensure_rule "$firewall_command" ZATA-DOCKER-INGRESS \
    -p tcp -m conntrack --ctstate NEW --ctorigdstport 80 -j ACCEPT
  ensure_rule "$firewall_command" ZATA-DOCKER-INGRESS \
    -p tcp -m conntrack --ctstate NEW --ctorigdstport 443 -j ACCEPT
  ensure_rule "$firewall_command" ZATA-DOCKER-INGRESS -j DROP

  if ! "$firewall_command" -w 10 -C DOCKER-USER \
    -i "$external_interface" -j ZATA-DOCKER-INGRESS 2>/dev/null; then
    "$firewall_command" -w 10 -I DOCKER-USER 1 \
      -i "$external_interface" -j ZATA-DOCKER-INGRESS
  fi
done

if ! iptables -w 10 -C DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null; then
  iptables -w 10 -I DOCKER-USER 1 -d 169.254.169.254/32 -j DROP
fi
if ! iptables -w 10 -C OUTPUT -d 169.254.169.254/32 \
  -m owner ! --uid-owner 0 -j REJECT 2>/dev/null; then
  iptables -w 10 -I OUTPUT 1 -d 169.254.169.254/32 \
    -m owner ! --uid-owner 0 -j REJECT
fi
EOF
  "${SUDO[@]}" chmod 0750 /usr/local/sbin/zata-docker-user-firewall

  "${SUDO[@]}" tee /etc/systemd/system/zata-docker-user-firewall.service >/dev/null <<'EOF'
[Unit]
Description=Restrict Docker ingress and instance metadata access
Requires=docker.service
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/zata-docker-user-firewall
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

  "${SUDO[@]}" systemctl daemon-reload
  "${SUDO[@]}" systemctl enable --now zata-docker-user-firewall.service
}

resolve_https_redirect_default() {
  if [[ -z "$ENABLE_HTTPS_REDIRECT" ]]; then
    if [[ -n "$ACME_EMAIL" ]]; then
      ENABLE_HTTPS_REDIRECT="true"
    else
      ENABLE_HTTPS_REDIRECT="false"
    fi
  fi

  case "$ENABLE_HTTPS_REDIRECT" in
    true|false) ;;
    *) die "ENABLE_HTTPS_REDIRECT 只能是 true 或 false。" ;;
  esac
}

create_traefik_network() {
  if "${SUDO[@]}" docker network inspect "$TRAEFIK_NETWORK" >/dev/null 2>&1; then
    log "Docker 网络 ${TRAEFIK_NETWORK} 已存在。"
    return
  fi

  log "创建 Docker 网络：${TRAEFIK_NETWORK}"
  "${SUDO[@]}" docker network create "$TRAEFIK_NETWORK"
}

write_traefik_files() {
  resolve_https_redirect_default

  log "写入 Traefik 配置到 ${TRAEFIK_DIR}"
  "${SUDO[@]}" mkdir -p "${TRAEFIK_DIR}/dynamic" "${TRAEFIK_DIR}/letsencrypt" "${TRAEFIK_DIR}/examples"
  "${SUDO[@]}" touch "${TRAEFIK_DIR}/letsencrypt/acme.json"
  "${SUDO[@]}" chmod 600 "${TRAEFIK_DIR}/letsencrypt/acme.json"

  "${SUDO[@]}" tee "${TRAEFIK_DIR}/.env" >/dev/null <<EOF
TRAEFIK_IMAGE=${TRAEFIK_IMAGE}
EOF

  "${SUDO[@]}" tee "${TRAEFIK_DIR}/docker-compose.yml" >/dev/null <<EOF
services:
  traefik:
    image: \${TRAEFIK_IMAGE}
    container_name: traefik
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    networks:
      - ${TRAEFIK_NETWORK}
    ports:
      - "80:80"
      - "443:443"
      - "127.0.0.1:8080:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik.yml:/etc/traefik/traefik.yml:ro
      - ./dynamic:/etc/traefik/dynamic:ro
      - ./letsencrypt:/letsencrypt
    healthcheck:
      test: ["CMD", "traefik", "healthcheck", "--ping"]
      interval: 30s
      timeout: 5s
      retries: 3

networks:
  ${TRAEFIK_NETWORK}:
    name: ${TRAEFIK_NETWORK}
    external: true
EOF

  "${SUDO[@]}" tee "${TRAEFIK_DIR}/traefik.yml" >/dev/null <<EOF
api:
  dashboard: true
  insecure: true

log:
  level: INFO

accessLog: {}

ping: {}

entryPoints:
  web:
    address: ":80"
EOF

  if [[ "$ENABLE_HTTPS_REDIRECT" == "true" ]]; then
    "${SUDO[@]}" tee -a "${TRAEFIK_DIR}/traefik.yml" >/dev/null <<'EOF'
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
          permanent: true
EOF
  fi

  "${SUDO[@]}" tee -a "${TRAEFIK_DIR}/traefik.yml" >/dev/null <<EOF
  websecure:
    address: ":443"

providers:
  docker:
    endpoint: "unix:///var/run/docker.sock"
    exposedByDefault: false
    network: "${TRAEFIK_NETWORK}"
  file:
    directory: "/etc/traefik/dynamic"
    watch: true
EOF

  if [[ -n "$ACME_EMAIL" ]]; then
    "${SUDO[@]}" tee -a "${TRAEFIK_DIR}/traefik.yml" >/dev/null <<EOF

certificatesResolvers:
  letsencrypt:
    acme:
      email: "${ACME_EMAIL}"
      storage: "/letsencrypt/acme.json"
      httpChallenge:
        entryPoint: web
EOF
  fi

  write_whoami_example
}

write_whoami_example() {
  if [[ -n "$ACME_EMAIL" ]]; then
    "${SUDO[@]}" tee "${TRAEFIK_DIR}/examples/whoami.compose.yml" >/dev/null <<EOF
services:
  whoami:
    image: traefik/whoami:v1.11
    restart: unless-stopped
    networks:
      - ${TRAEFIK_NETWORK}
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.whoami.rule=Host(\`${WHOAMI_HOST}\`)"
      - "traefik.http.routers.whoami.entrypoints=websecure"
      - "traefik.http.routers.whoami.tls=true"
      - "traefik.http.routers.whoami.tls.certresolver=letsencrypt"

networks:
  ${TRAEFIK_NETWORK}:
    external: true
EOF
  else
    "${SUDO[@]}" tee "${TRAEFIK_DIR}/examples/whoami.compose.yml" >/dev/null <<EOF
services:
  whoami:
    image: traefik/whoami:v1.11
    restart: unless-stopped
    networks:
      - ${TRAEFIK_NETWORK}
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.whoami.rule=Host(\`${WHOAMI_HOST}\`)"
      - "traefik.http.routers.whoami.entrypoints=web"

networks:
  ${TRAEFIK_NETWORK}:
    external: true
EOF
  fi
}

start_traefik() {
  log "启动 Traefik。"
  "${SUDO[@]}" docker compose \
    --env-file "${TRAEFIK_DIR}/.env" \
    -f "${TRAEFIK_DIR}/docker-compose.yml" \
    up -d
}

install_sample_if_requested() {
  [[ "$INSTALL_SAMPLE" == "true" ]] || return

  log "启动 whoami 示例服务：${WHOAMI_HOST}"
  "${SUDO[@]}" docker compose \
    -f "${TRAEFIK_DIR}/examples/whoami.compose.yml" \
    -p whoami \
    up -d
}

print_summary() {
  cat <<EOF

完成。

Traefik 配置目录：
  ${TRAEFIK_DIR}

常用命令：
  sudo docker compose --env-file ${TRAEFIK_DIR}/.env -f ${TRAEFIK_DIR}/docker-compose.yml ps
  sudo docker compose --env-file ${TRAEFIK_DIR}/.env -f ${TRAEFIK_DIR}/docker-compose.yml logs -f
  sudo docker compose --env-file ${TRAEFIK_DIR}/.env -f ${TRAEFIK_DIR}/docker-compose.yml restart

Dashboard 默认只监听服务器本机：
  http://127.0.0.1:8080/dashboard/

远程查看 dashboard 可用 SSH 端口转发：
  ssh -L 8080:127.0.0.1:8080 user@server

示例服务配置已生成：
  ${TRAEFIK_DIR}/examples/whoami.compose.yml

EOF

  if [[ -z "$ACME_EMAIL" ]]; then
    cat <<EOF
提示：本次把 ACME_EMAIL 设为空，所以没有启用 Let's Encrypt 证书解析器。
  - 当前 Traefik 只能提供 HTTP（80 端口），HTTPS 会返回自签证书。
  - 想启用真证书，先把 DNS 解析指到本机，再重跑：
      ACME_EMAIL=you@your-domain.com bash $(basename "$0")
    脚本是幂等的，会自动追加 certificatesResolvers 段并重启 Traefik。

EOF
  else
    cat <<EOF
Let's Encrypt 证书解析器已启用（ACME_EMAIL=${ACME_EMAIL}）。
  - DNS 必须先把要签发的域名解析到本机，否则 LE HTTP challenge 拿不到 80。
  - 第一个外部 HTTPS 请求触发后，Traefik 会自动向 LE 申请证书（通常 30 秒内）。
  - 想看 LE 流程：sudo docker logs -f \$(sudo docker ps -q --filter ancestor=${TRAEFIK_IMAGE})
  - 证书存放在 ${TRAEFIK_DIR}/letsencrypt/acme.json，Traefik 自动续期。
  - 证书异常时先看 docker logs 里的 acme 报错，再用 fix-acme-email.sh 修正邮箱。

EOF

    if [[ "$ACME_EMAIL" == "$DEFAULT_ACME_EMAIL" ]]; then
      cat <<EOF
提示：当前使用默认占位邮箱 ${DEFAULT_ACME_EMAIL}。它不接收邮件，也不会被 Let's Encrypt 拒绝，
  所以证书能正常签发；想改成自己的邮箱时重跑本脚本并传 ACME_EMAIL=you@your-domain.com。

EOF
    fi
  fi
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  require_sudo

  # Fail fast if the user passed a known placeholder email. Let's Encrypt
  # silently rejects registrations with these, the cert never gets issued,
  # and Traefik falls back to its default self-signed cert (which browsers
  # mark as "invalid"). Reject up front with a clear pointer to the fix.
  if [[ -n "$ACME_EMAIL" ]]; then
    if [[ "$ACME_EMAIL" =~ @(example\.com|example\.org|example\.net|localhost)$ ]]; then
      die "ACME_EMAIL='$ACME_EMAIL' 看起来是占位邮箱。Let's Encrypt 不会接受 example.com / localhost 等保留域名，会导致证书申请失败、浏览器报'此网站的证书无效'。请传一个真实邮箱，例如 ACME_EMAIL=you@your-domain.com"
    fi
    if ! [[ "$ACME_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
      die "ACME_EMAIL='$ACME_EMAIL' 格式不像合法邮箱。"
    fi
  fi

  install_docker
  configure_docker_firewall
  create_traefik_network
  write_traefik_files
  start_traefik
  install_sample_if_requested
  print_summary
}

main "$@"
