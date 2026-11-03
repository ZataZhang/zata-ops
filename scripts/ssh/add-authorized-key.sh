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
#   SSH_KEY        本地用于登录的私钥路径（默认 ~/.ssh/id_ed25519）
#   SSH_PORT       目标服务器 SSH 端口（默认 22）
#   SSH_TIMEOUT    单次 SSH 命令总超时秒数（默认 20；密码引导模式默认 60）
#   SSH_ALLOW_PASSWORD
#                 是否允许交互式密码引导（默认 0；首次给服务器加公钥时可设为 1）
#
# 超时行为：
#   - TCP 握手阶段：最多 10 秒（ConnectTimeout）
#   - 单次 SSH 命令总耗时：默认最多 20 秒；密码引导模式默认 60 秒（SSH_TIMEOUT）
#   - 默认不允许任何交互式密码 / passphrase 提示（BatchMode=yes + NumberOfPasswordPrompts=0），
#     避免脚本在本地公钥未注册到服务器时"看起来卡住"
#   - 设置 SSH_ALLOW_PASSWORD=1 后允许交互式密码登录，用于首次写入 authorized_keys
#
# 退出码：
#   0  公钥已成功追加
#   1  参数缺失或公钥格式不合法
#   2  SSH 连接失败
#   3  远程目录或文件操作失败
set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly DEFAULT_SSH_USER="${SSH_USER:-root}"
readonly DEFAULT_SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
readonly DEFAULT_SSH_PORT="${SSH_PORT:-22}"
readonly DEFAULT_SSH_TIMEOUT="${SSH_TIMEOUT:-20}"
readonly DEFAULT_SSH_ALLOW_PASSWORD="${SSH_ALLOW_PASSWORD:-0}"

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

run_ssh() {
  local ssh_timeout="$1"
  local ssh_status
  shift

  perl -e '
    use strict;
    use warnings;

    my $timeout = shift @ARGV;
    my $child_pid = fork();
    die "fork failed: $!\n" unless defined $child_pid;

    if ($child_pid == 0) {
      exec @ARGV or die "exec failed: $!\n";
    }

    local $SIG{ALRM} = sub {
      kill "TERM", $child_pid;
      select undef, undef, undef, 0.2;
      kill "KILL", $child_pid;
      exit 124;
    };

    alarm $timeout;
    waitpid($child_pid, 0);
    my $status = $?;
    alarm 0;

    if ($status & 127) {
      exit(128 + ($status & 127));
    }
    exit($status >> 8);
  ' "$ssh_timeout" ssh "$@"
  ssh_status=$?

  if [[ "$ssh_status" -eq 124 ]]; then
    log "ERROR: SSH 命令超过 ${ssh_timeout}s，已中断" >&2
  fi

  return "$ssh_status"
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
  local ssh_timeout="$DEFAULT_SSH_TIMEOUT"
  local ssh_allow_password="$DEFAULT_SSH_ALLOW_PASSWORD"
  local remote_status=0
  local remote_add_key_script

  require_command ssh
  require_command perl

  # 1. 校验公钥格式：必须以合法算法标识开头，且至少包含两个空白分隔的字段。
  if ! [[ "$public_key" =~ ^[A-Za-z0-9._@+-]+[[:space:]]+[A-Za-z0-9+/=]+([[:space:]]+.*)?$ ]]; then
    fail "公钥格式不合法，请粘贴完整的 SSH 公钥（包含类型与 base64 主体）"
  fi

  if ! [[ "$ssh_allow_password" =~ ^[01]$ ]]; then
    fail "SSH_ALLOW_PASSWORD 只能是 0 或 1：$ssh_allow_password"
  fi

  if [[ "$ssh_allow_password" == "1" && -z "${SSH_TIMEOUT:-}" ]]; then
    ssh_timeout=60
  fi

  if ! [[ "$ssh_timeout" =~ ^[1-9][0-9]*$ ]]; then
    fail "SSH_TIMEOUT 必须是正整数秒数：$ssh_timeout"
  fi

  # 2. 校验本地私钥存在。
  if [[ ! -f "$ssh_key" ]]; then
    fail "本地私钥不存在：$ssh_key（可通过 SSH_KEY 环境变量覆盖）"
  fi

  log "目标服务器：${ssh_user}@${target_host}:${ssh_port}"
  log "本地私钥：${ssh_key}"
  log "单次 SSH 超时：${ssh_timeout}s"
  if [[ "$ssh_allow_password" == "1" ]]; then
    log "密码引导：已启用（首次连接会要求输入 ${ssh_user} 密码）"
  fi

  # ssh_opts 设计要点：
  #   - BatchMode=yes + NumberOfPasswordPrompts=0：拒绝任何交互式密码/passphrase 提示，
  #     避免 BatchMode 在某些 OpenSSH 版本下仍然尝试问密码导致脚本"看起来卡住"。
  #   - ConnectTimeout=10：限制 TCP 握手阶段超时。
  #   - IdentitiesOnly=yes + PreferredAuthentications=publickey：只尝试指定私钥，避免
  #     ssh-agent 中其他身份或键盘交互认证拖慢失败路径。
  #   - StrictHostKeyChecking=accept-new：未知 host key 直接接受并发警告；已变更的
  #     host key 严格拒绝，防止中间人攻击。
  local -a ssh_opts=(
    -i "$ssh_key"
    -p "$ssh_port"
    -o ConnectTimeout=10
    -o ConnectionAttempts=1
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=accept-new
  )

  if [[ "$ssh_allow_password" == "1" ]]; then
    ssh_opts+=(
      -o NumberOfPasswordPrompts=3
      -o PreferredAuthentications=publickey,password,keyboard-interactive
    )
  else
    ssh_opts+=(
      -o BatchMode=yes
      -o NumberOfPasswordPrompts=0
      -o PreferredAuthentications=publickey
    )
  fi

  remote_add_key_script='
set -eu

IFS= read -r public_key
ssh_dir="$HOME/.ssh"
auth_file="$ssh_dir/authorized_keys"

mkdir -p "$ssh_dir"
chmod 700 "$ssh_dir"
touch "$auth_file"
chmod 600 "$auth_file"

if grep -qxF "$public_key" "$auth_file"; then
  exit 10
fi

printf "%s\n" "$public_key" >> "$auth_file"
chmod 600 "$auth_file"
'

  # 3. 一次 SSH 调用完成目录准备、整行查重和追加，避免中间步骤失败后误报成功。
  if run_ssh "$ssh_timeout" "${ssh_opts[@]}" "${ssh_user}@${target_host}" "$remote_add_key_script" <<< "$public_key"; then
    log "公钥已成功添加到 ${ssh_user}@${target_host}"
    exit 0
  else
    remote_status=$?
  fi

  if [[ "$remote_status" -eq 10 ]]; then
    log "该公钥已存在于 authorized_keys，跳过追加"
    exit 0
  fi

  if [[ "$remote_status" -eq 124 || "$remote_status" -eq 255 ]]; then
    fail "无法通过 ${ssh_user}@${target_host} 完成 SSH 操作；若这是首次配置且你有 root 密码，请设置 SSH_ALLOW_PASSWORD=1 后重试" 2
  fi

  fail "远程 authorized_keys 操作失败，退出码：$remote_status" 3
}

main "$@"
