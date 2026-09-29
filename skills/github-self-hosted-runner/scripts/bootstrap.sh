#!/usr/bin/env bash
# Run on the target Linux host as root.
# Required: RUNNER_URL, RUNNER_NAME, RUNNER_VERSION, RUNNER_SHA256.
# Required for register: RUNNER_LABELS (a project or purpose label).
# Optional: RUNNER_USER (github-runner), RUNNER_DIR
#           (/opt/actions-runner). RUNNER_DIR may be on a data disk.
# Usage: bootstrap.sh prepare|register|service|status
# Pass the one-time registration token to `register` on stdin.
set -euo pipefail

action="${1:-}"
runner_user="${RUNNER_USER:-github-runner}"
runner_dir="${RUNNER_DIR:-/opt/actions-runner}"

die() { printf 'runner bootstrap: %s\n' "$*" >&2; exit 1; }

require_root() { [[ "$(id -u)" -eq 0 ]] || die 'run as root'; }

require_paths() {
  [[ "$runner_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'RUNNER_USER contains unsupported characters'
  [[ "$runner_dir" =~ ^/[A-Za-z0-9_./-]+$ && "$runner_dir" != / ]] || die 'RUNNER_DIR must be a simple absolute directory'
}

require_settings() {
  [[ -n "${RUNNER_URL:-}" && -n "${RUNNER_NAME:-}" ]] || die 'set RUNNER_URL and RUNNER_NAME'
  [[ "$RUNNER_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$ ]] || die 'RUNNER_URL must be a GitHub organization or repository URL'
  [[ "$RUNNER_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die 'RUNNER_NAME contains unsupported characters'
}

runner_arch() {
  case "$(uname -m)" in
    x86_64) printf 'x64' ;;
    aarch64) printf 'arm64' ;;
    *) die 'unsupported Linux architecture' ;;
  esac
}

prepare() {
  [[ "$(uname -s)" = Linux ]] || die 'Linux is required'
  [[ -n "${RUNNER_VERSION:-}" && "${RUNNER_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]] || die 'set RUNNER_VERSION and a 64-character RUNNER_SHA256'
  [[ "$RUNNER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'RUNNER_VERSION must be a release number'
  for command_name in curl tar sha256sum useradd install mktemp runuser; do
    command -v "$command_name" >/dev/null || die "missing $command_name"
  done

  if [[ -e "$runner_dir/config.sh" ]]; then
    printf 'runner already installed at %s; leaving it unchanged\n' "$runner_dir"
    return
  fi
  if [[ -e "$runner_dir" && -n "$(ls -A "$runner_dir")" ]]; then
    die "existing nonempty directory: $runner_dir"
  fi
  if ! id "$runner_user" >/dev/null 2>&1; then
    useradd --create-home --shell /usr/sbin/nologin "$runner_user"
  fi
  install -d -m 0755 -o "$runner_user" -g "$runner_user" "$runner_dir"
  runuser -u "$runner_user" -- test -w "$runner_dir" \
    || die "runner user cannot access $runner_dir or one of its parent directories"

  local archive_name archive_path
  archive_name="actions-runner-linux-$(runner_arch)-${RUNNER_VERSION}.tar.gz"
  archive_path="$(mktemp)"
  trap 'rm -f "$archive_path"' EXIT
  curl --fail --location --retry 3 --output "$archive_path" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${archive_name}"
  printf '%s  %s\n' "$RUNNER_SHA256" "$archive_path" | sha256sum --check --status \
    || die 'runner archive SHA-256 mismatch'
  chmod 0644 "$archive_path"
  runuser -u "$runner_user" -- tar -xzf "$archive_path" -C "$runner_dir"
  [[ -x "$runner_dir/config.sh" ]] || die 'runner archive did not contain config.sh'
  "$runner_dir/bin/installdependencies.sh"
  printf 'runner package and dependencies installed at %s\n' "$runner_dir"
}

register() {
  [[ -x "$runner_dir/config.sh" ]] || die 'run prepare first'
  [[ ! -e "$runner_dir/.runner" ]] || die 'runner is already registered; inspect .runner before changing identity'
  [[ -n "${RUNNER_LABELS:-}" && "$RUNNER_LABELS" =~ ^[A-Za-z0-9_,.-]+$ ]] || die 'set RUNNER_LABELS to a project or purpose label'
  local registration_token
  IFS= read -r -s registration_token || die 'registration token missing on stdin'
  [[ -n "$registration_token" ]] || die 'registration token is empty'
  local -a config_args=(--unattended --url "$RUNNER_URL" --token "$registration_token" --name "$RUNNER_NAME" --work _work)
  config_args+=(--labels "$RUNNER_LABELS")
  (cd "$runner_dir" && runuser -u "$runner_user" -- ./config.sh "${config_args[@]}")
  unset registration_token
}

service() {
  [[ -e "$runner_dir/.runner" ]] || die 'run register first'
  [[ -x "$runner_dir/svc.sh" ]] || die 'svc.sh missing'
  if [[ ! -f "$runner_dir/.service" ]]; then
    (cd "$runner_dir" && ./svc.sh install "$runner_user")
  fi
  local service_name
  service_name="$(cat "$runner_dir/.service")"
  [[ "$service_name" =~ ^actions\.runner\.[A-Za-z0-9_.-]+\.service$ ]] || die 'unexpected runner service name'
  install -d -m 0755 "/etc/systemd/system/${service_name}.d"
  printf '[Unit]\nRequiresMountsFor=%s\n' "$runner_dir" \
    > "/etc/systemd/system/${service_name}.d/10-storage.conf"
  systemctl daemon-reload
  systemctl enable --now "$service_name"
  systemctl is-active --quiet "$service_name" || die 'runner service failed to start'
  printf 'active service: %s\n' "$service_name"
}

status() {
  [[ -f "$runner_dir/.service" ]] || die 'runner service is not installed'
  local service_name
  service_name="$(cat "$runner_dir/.service")"
  systemctl is-enabled "$service_name"
  systemctl is-active "$service_name"
  journalctl -u "$service_name" -n 12 --no-pager
}

case "$action" in
  prepare|register) require_root; require_paths; require_settings; "$action" ;;
  service|status) require_root; require_paths; "$action" ;;
  *) die 'usage: bootstrap.sh prepare|register|service|status' ;;
esac
