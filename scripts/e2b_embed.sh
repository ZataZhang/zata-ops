#!/usr/bin/env bash
set -euo pipefail

ops_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
vm_name="zata-e2b-embed"
vm_config="$ops_root/local/e2b-embed/lima.yaml"
guest_manager="$ops_root/local/e2b-embed/manage.sh"
env_writer="$ops_root/scripts/e2b_embed_configure.py"
runtime_commit="0c21aa2277b59a1d040761ed3fbbb29a78775470"
runtime_source_dir="$ops_root/local/e2b-embed"
action="${1:-status}"
client_env_file="${2:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "E2B Embed local setup currently expects macOS with Lima and Apple virtualization." >&2
    exit 1
fi
if ! command -v limactl >/dev/null 2>&1; then
    echo "Lima is required. Install it with: brew install lima" >&2
    exit 1
fi

instance_exists() {
    limactl list --quiet | grep -Fxq "$vm_name"
}

start_vm() {
    if instance_exists; then
        limactl start --tty=false "$vm_name"
    else
        limactl start --tty=false --name="$vm_name" "$vm_config"
    fi
}

run_guest_manager() {
    limactl shell "$vm_name" -- sudo \
        --preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy \
        bash -s -- "$1" "$runtime_commit" < "$guest_manager"
}

case "$action" in
    up)
        start_vm
        for required_device in /dev/kvm /dev/net/tun; do
            if ! limactl shell "$vm_name" -- test -c "$required_device"; then
                echo "E2B Embed requires $required_device inside the Lima VM; nested virtualization is unavailable." >&2
                exit 1
            fi
        done
        limactl shell "$vm_name" -- mkdir -p /tmp/zata-ops-e2b-embed
        limactl copy "$runtime_source_dir/compose.yaml" \
            "$vm_name:/tmp/zata-ops-e2b-embed/compose.yaml"
        limactl copy "$runtime_source_dir/e2b-embed.env.example" \
            "$vm_name:/tmp/zata-ops-e2b-embed/e2b-embed.env.example"
        run_guest_manager up
        if [[ -n "$client_env_file" ]]; then
            if [[ ! -f "$client_env_file" ]]; then
                app_repo_root="$(cd "$ops_root/../zata_code_template" && pwd -P)"
                cp "$app_repo_root/.env.example" "$client_env_file"
            fi
            client_env_dir="$(cd "$(dirname "$client_env_file")" && pwd -P)"
            client_env_path="$client_env_dir/$(basename "$client_env_file")"
            sdk_environment_file="$(mktemp "${TMPDIR:-/tmp}/e2b-sdk-env.XXXXXX")"
            chmod 600 "$sdk_environment_file"
            trap 'rm -f "$sdk_environment_file"' EXIT
            run_guest_manager sdk-env > "$sdk_environment_file"
            python3 "$env_writer" "$client_env_path" < "$sdk_environment_file"
            rm -f "$sdk_environment_file"
            trap - EXIT
        else
            echo "E2B Embed is ready on 127.0.0.1:3000 (API), :3001 (dashboard), and :3002 (sandbox proxy)."
            echo "Pass your application's .env.local path to write the SDK settings automatically."
        fi
        ;;
    down)
        if instance_exists; then
            run_guest_manager down
        else
            echo "The E2B Embed Lima VM is not installed."
        fi
        ;;
    stop)
        if ! instance_exists; then
            echo "The E2B Embed Lima VM is not installed."
        elif limactl list --filter '.status == "Running"' --quiet | grep -Fxq "$vm_name"; then
            limactl stop "$vm_name"
            echo "E2B Embed VM stopped; databases, templates and the API key stay on its disk."
            echo "Start it again with: scripts/e2b_embed.sh up"
        else
            echo "The E2B Embed Lima VM is already stopped."
        fi
        ;;
    status)
        if instance_exists; then
            limactl list "$vm_name"
            if limactl list --filter '.status == "Running"' --quiet | grep -Fxq "$vm_name"; then
                run_guest_manager status
            fi
        else
            echo "E2B Embed Lima VM is not installed. Run: scripts/e2b_embed.sh up"
        fi
        ;;
    logs)
        if ! instance_exists || ! limactl list --filter '.status == "Running"' --quiet | grep -Fxq "$vm_name"; then
            echo "The E2B Embed Lima VM is not running. Run: scripts/e2b_embed.sh up" >&2
            exit 1
        fi
        run_guest_manager logs
        ;;
    *)
        echo "Usage: scripts/e2b_embed.sh [up [application-env-file]|status|logs|down|stop]" >&2
        exit 2
        ;;
esac
