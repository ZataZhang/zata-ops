#!/usr/bin/env bash
set -euo pipefail

action="${1:-status}"
runtime_commit="${2:?E2B runtime commit is required}"
stack_dir="/var/lib/zata-ops/e2b-embed"
source_dir="/tmp/zata-ops-e2b-embed"

configure_proxy() {
    local http_proxy_url="${http_proxy:-${HTTP_PROXY:-}}"
    local https_proxy_url="${https_proxy:-${HTTPS_PROXY:-$http_proxy_url}}"
    local no_proxy_hosts="${no_proxy:-${NO_PROXY:-}}"
    if [ -z "$http_proxy_url" ] && [ -z "$https_proxy_url" ]; then
        return
    fi

    export E2B_LOCAL_HTTP_PROXY="$http_proxy_url"
    export E2B_LOCAL_HTTPS_PROXY="$https_proxy_url"
    export E2B_LOCAL_NO_PROXY="localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal${no_proxy_hosts:+,$no_proxy_hosts}"
    proxy_config_status="$(python3 - <<'PY'
import json
import os
import tempfile
from pathlib import Path

config_path = Path("/etc/docker/daemon.json")
config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
proxy_settings = {
    "http-proxy": os.environ["E2B_LOCAL_HTTP_PROXY"],
    "https-proxy": os.environ["E2B_LOCAL_HTTPS_PROXY"],
    "no-proxy": os.environ["E2B_LOCAL_NO_PROXY"],
}
if config.get("proxies") == proxy_settings:
    print("unchanged")
else:
    config["proxies"] = proxy_settings
    file_descriptor, temporary_name = tempfile.mkstemp(dir=config_path.parent)
    temporary_path = Path(temporary_name)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as config_file_handle:
        json.dump(config, config_file_handle, indent=2)
        config_file_handle.write("\n")
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(config_path)
    print("changed")
PY
)"
    if [ "$proxy_config_status" = "changed" ]; then
        systemctl restart docker
    fi
}

install_compose_files() {
    if [ ! -f "$source_dir/compose.yaml" ] || [ ! -f "$source_dir/e2b-embed.env.example" ]; then
        echo "E2B Embed manifests were not copied from zata-ops into this Lima VM." >&2
        exit 1
    fi

    if [ -f "$stack_dir/runtime.commit" ]; then
        installed_commit="$(cat "$stack_dir/runtime.commit")"
        if [ "$installed_commit" != "$runtime_commit" ]; then
            echo "E2B Embed was installed from a different pinned runtime commit." >&2
            echo "Installed: $installed_commit" >&2
            echo "Requested: $runtime_commit" >&2
            echo "Refusing an implicit upgrade; back up the named volumes before changing the pin." >&2
            exit 1
        fi
    fi

    install -d -m 0755 "$stack_dir"
    temp_dir="$(mktemp -d "$stack_dir/.runtime.XXXXXX")"
    trap 'rm -rf "$temp_dir"' RETURN
    install -m 0644 "$source_dir/compose.yaml" "$temp_dir/compose.yaml"
    install -m 0644 "$source_dir/e2b-embed.env.example" "$temp_dir/.env"
    printf '%s\n' "$runtime_commit" > "$temp_dir/runtime.commit"
    chmod 0644 "$temp_dir/compose.yaml" "$temp_dir/.env" "$temp_dir/runtime.commit"
    mv "$temp_dir/compose.yaml" "$stack_dir/compose.yaml"
    mv "$temp_dir/.env" "$stack_dir/.env"
    mv "$temp_dir/runtime.commit" "$stack_dir/runtime.commit"
    if [ ! -f "$stack_dir/.env.local" ]; then
        install -m 0600 /dev/null "$stack_dir/.env.local"
    fi
    rmdir "$temp_dir"
    trap - RETURN
}

docker_compose() {
    docker compose \
        --env-file "$stack_dir/.env" \
        --env-file "$stack_dir/.env.local" \
        "$@"
}

if [ "$action" = "up" ]; then
    configure_proxy
    install_compose_files
    cat > "$stack_dir/compose.override.yaml" <<'COMPOSE_OVERRIDE'
services:
  fetch-artifacts:
    environment:
      HTTP_PROXY: ${https_proxy:-}
      HTTPS_PROXY: ${https_proxy:-}
      NO_PROXY: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
      http_proxy: ${http_proxy:-}
      https_proxy: ${https_proxy:-}
      no_proxy: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
  orchestrator:
    environment:
      HTTP_PROXY: ${https_proxy:-}
      HTTPS_PROXY: ${https_proxy:-}
      NO_PROXY: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
      http_proxy: ${http_proxy:-}
      https_proxy: ${https_proxy:-}
      no_proxy: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
  base-template:
    environment:
      HTTP_PROXY: ${https_proxy:-}
      HTTPS_PROXY: ${https_proxy:-}
      NO_PROXY: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
      http_proxy: ${http_proxy:-}
      https_proxy: ${https_proxy:-}
      no_proxy: ${no_proxy:-localhost,127.0.0.1,::1,host.lima.internal,host.docker.internal}
COMPOSE_OVERRIDE
elif [ ! -f "$stack_dir/compose.yaml" ] || [ ! -f "$stack_dir/.env" ]; then
    if [ "$action" = "status" ]; then
        echo "E2B Embed is not installed in this Lima VM. Run on the host: scripts/e2b_embed.sh up"
        exit 0
    fi
    echo "E2B Embed has not been installed. Run on the host: scripts/e2b_embed.sh up" >&2
    exit 1
fi

cd "$stack_dir"
if [ ! -f "$stack_dir/.env.local" ]; then
    install -m 0600 /dev/null "$stack_dir/.env.local"
fi

case "$action" in
    up)
        docker_compose up -d --wait
        ;;
    down)
        docker_compose down
        ;;
    status)
        docker_compose ps
        ;;
    logs)
        docker_compose logs --follow --tail=100
        ;;
    sdk-env)
        docker_compose exec -T ready cat /run/e2b/sdk.env
        ;;
    *)
        echo "Unknown E2B Embed action: $action" >&2
        exit 2
        ;;
esac
