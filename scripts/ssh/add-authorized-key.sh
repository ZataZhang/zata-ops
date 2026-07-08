#!/usr/bin/env bash
# scripts/ssh/add-authorized-key.sh
#
# 用途：以 root + 本地默认私钥登录目标服务器，把传入的 SSH 公钥追加到该服务器的
#       `/root/.ssh/authorized_keys` 中，使持有该公钥对应私钥的电脑也能登录。
#
# 用法：
#   scripts/ssh/add-authorized-key.sh <server-ip> "<ssh-public-key>"
#
# 示例：
#   scripts/ssh/add-authorized-key.sh 10.0.0.5 "ssh-ed25519 AAAA... user@host"
#
# 可选环境变量：
#   SSH_USER       目标服务器登录用户（默认 root）
#   SSH_KEY        本地用于登录的私钥路径（默认 ~/.ssh/id_rsa）
#   SSH_PORT       目标服务器 SSH 端口（默认 22）
#
# 退出码：
#   0  公钥已成功追加
#   1  参数缺失或公钥格式不合法
#   2  SSH 连接失败
#   3  远程目录或文件操作失败
set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly DEFAULT_SSH_USER="${SSH_USER:-root}"
readonly DEFAULT_SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
readonly DEFAULT_SSH_PORT="${SSH_PORT:-22}"

log() {
  printf '[%s] %s\n' "$SCRIPT_NAME" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit "${2:-1}"
}

require_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "缺少依赖命令：$cmd"
}

usage() {
  sed -n '2,20p' "$0"
  exit 1
}

main() {
  [[ $# -ge 2 ]] || usage

  local target_host="$1"
  local public_key="$2"
  local ssh_user="$DEFAULT_SSH_USER"
  local ssh_key="$DEFAULT_SSH_KEY"
  local ssh_port="$DEFAULT_SSH_PORT"

  require_command ssh
  require_command scp

  # 1. 校验公钥格式：必须以合法算法标识开头，且至少包含两个空白分隔的字段。
  if ! [[ "$public_key" =~ ^[A-Za-z0-9._@+-]+[[:space:]]+[A-Za-z0-9+/=]+([[:space:]]+.*)?$ ]]; then
    fail "公钥格式不合法，请粘贴完整的 SSH 公钥（包含类型与 base64 主体）"
  fi

  # 2. 校验本地私钥存在。
  if [[ ! -f "$ssh_key" ]]; then
    fail "本地私钥不存在：$ssh_key（可通过 SSH_KEY 环境变量覆盖）"
  fi

  log "目标服务器：${ssh_user}@${target_host}:${ssh_port}"
  log "本地私钥：${ssh_key}"

  local -a ssh_opts=(
    -i "$ssh_key"
    -p "$ssh_port"
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
  )

  # 3. 验证 SSH 连接可达，避免后续操作静默失败。
  if ! ssh "${ssh_opts[@]}" "${ssh_user}@${target_host}" true; then
    fail "无法通过 ${ssh_user}@${target_host} 建立 SSH 连接" 2
  fi

  # 4. 确保 ~/.ssh 存在并设置正确权限。
  if ! ssh "${ssh_opts[@]}" "${ssh_user}@${target_host}" \
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh"; then
    fail "无法在远程服务器上准备 ~/.ssh 目录" 3
  fi

  # 5. 检查目标公钥是否已存在，避免重复追加。
  local escaped_key="${public_key//\'/\'\\\'\'}"
  local check_cmd="grep -F -q '${escaped_key}' ~/.ssh/authorized_keys 2>/dev/null"

  if ssh "${ssh_opts[@]}" "${ssh_user}@${target_host}" "sh -c '$check_cmd'"; then
    log "该公钥已存在于 authorized_keys，跳过追加"
    exit 0
  fi

  # 6. 追加公钥并设置权限。
  local append_cmd="printf '%s\n' '${escaped_key}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
  if ! ssh "${ssh_opts[@]}" "${ssh_user}@${target_host}" "sh -c '$append_cmd'"; then
    fail "追加公钥到 authorized_keys 失败" 3
  fi

  log "公钥已成功添加到 ${ssh_user}@${target_host}"
}

main "$@"
