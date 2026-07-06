# ───────────────────────────────────────────────────────────────────────────────
# justfile — project-private recipes.
#
# Shared recipes live in `justfile.shared` and are kept in sync with the
# upstream template repository via `just sync-template`. Add or override
# project-specific commands below; same-name recipes here override shared ones.
# ───────────────────────────────────────────────────────────────────────────────

import 'justfile.shared'

# Run the development entrypoint for zata-ops: backend (FastAPI) + two frontends.
# Usage:
#   just run                         # backend + frontend-admin + frontend-public
#   just run backend                 # backend only
#   just run frontend                # frontend-admin + frontend-public
#   just run docker                  # via docker compose (待后续轮补 docker-compose.yml)
#   just run backend backend_port=8010 \
#                frontend_admin_port=5179 frontend_public_port=3001
run arg1="" arg2="" arg3="" arg4="" arg5="" arg6="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    backend_port=""
    frontend_admin_port=""
    frontend_public_port=""
    backend_cmd="uv run uvicorn backend.main:app --host 0.0.0.0 --port \${BACKEND_PORT:-8000} --reload"
    frontend_admin_cmd="pnpm dev -- --port \${FRONTEND_ADMIN_PORT:-5173} --strictPort"
    frontend_public_cmd="PORT=\${FRONTEND_PUBLIC_PORT:-3000} pnpm dev"

    repo_root="{{justfile_directory()}}"
    run_state_file="{{justfile_directory()}}/.env.run-state"

    backend_pid=""
    frontend_admin_pid=""
    frontend_public_pid=""

    parse_run_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then
            return 0
        fi

        case "$cli_arg" in
            target=*)
                target="${cli_arg#target=}"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_admin_port=*)
                frontend_admin_port="${cli_arg#frontend_admin_port=}"
                ;;
            frontend_public_port=*)
                frontend_public_port="${cli_arg#frontend_public_port=}"
                ;;
            backend_cmd=*)
                backend_cmd="${cli_arg#backend_cmd=}"
                ;;
            frontend_admin_cmd=*)
                frontend_admin_cmd="${cli_arg#frontend_admin_cmd=}"
                ;;
            frontend_public_cmd=*)
                frontend_public_cmd="${cli_arg#frontend_public_cmd=}"
                ;;
            *)
                case "$cli_arg" in
                    backend|frontend|all|docker)
                        target="$cli_arg"
                        ;;
                    *)
                        echo "ERROR: Unexpected run argument: $cli_arg"
                        echo "Usage: just run [backend|frontend|all|docker]"
                        echo "       [backend_port=<p>] [frontend_admin_port=<p>] [frontend_public_port=<p>]"
                        echo "       [backend_cmd=<cmd>] [frontend_admin_cmd=<cmd>] [frontend_public_cmd=<cmd>]"
                        exit 1
                        ;;
                esac
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}} {{quote(arg4)}} {{quote(arg5)}} {{quote(arg6)}}; do
        parse_run_arg "$cli_arg"
    done

    load_run_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi
        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_admin_port="${frontend_admin_port:-${FRONTEND_ADMIN_PORT:-5173}}"
        frontend_public_port="${frontend_public_port:-${FRONTEND_PUBLIC_PORT:-3000}}"
    }

    save_run_ports() {
        mkdir -p "$(dirname "$run_state_file")"
        {
            printf 'BACKEND_PORT=%s\n' "$backend_port"
            printf 'FRONTEND_ADMIN_PORT=%s\n' "$frontend_admin_port"
            printf 'FRONTEND_PUBLIC_PORT=%s\n' "$frontend_public_port"
        } > "$run_state_file"
    }

    assert_pnpm_installed() {
        if ! command -v pnpm >/dev/null 2>&1; then
            echo "ERROR: pnpm 未安装。frontend-admin/frontend-public 依赖 pnpm workspace。"
            echo "   安装: npm i -g pnpm@11.3.0"
            exit 1
        fi
    }

    assert_backend_extra() {
        if ! uv run --no-sync python -c "import fastapi, uvicorn, alembic, sqlalchemy" >/dev/null 2>&1; then
            echo "ERROR: backend 依赖未安装。请先: uv sync --extra backend"
            exit 1
        fi
    }

    run_backend() {
        echo "Starting backend (uvicorn backend.main:app) on port $backend_port"
        (
            cd "$repo_root"
            PORT="$backend_port" bash -lc "$backend_cmd"
        )
    }

    run_frontend_admin() {
        echo "Starting frontend-admin (Vite) on port $frontend_admin_port"
        (
            cd "$repo_root/frontend-admin"
            BACKEND_PORT="$backend_port" \
            FRONTEND_ADMIN_PORT="$frontend_admin_port" \
            FRONTEND_PUBLIC_PORT="$frontend_public_port" \
            bash -lc "$frontend_admin_cmd"
        )
    }

    run_frontend_public() {
        echo "Starting frontend-public (Next.js) on port $frontend_public_port"
        (
            cd "$repo_root/frontend-public"
            BACKEND_PORT="$backend_port" \
            FRONTEND_ADMIN_PORT="$frontend_admin_port" \
            FRONTEND_PUBLIC_PORT="$frontend_public_port" \
            bash -lc "$frontend_public_cmd"
        )
    }

    cleanup_processes() {
        for process_pid in "$backend_pid" "$frontend_admin_pid" "$frontend_public_pid"; do
            if [ -n "$process_pid" ] && kill -0 "$process_pid" 2>/dev/null; then
                kill "$process_pid" 2>/dev/null || true
            fi
        done
        wait 2>/dev/null || true
    }

    wait_for_first_exit() {
        while true; do
            for process_pid in "$backend_pid" "$frontend_admin_pid" "$frontend_public_pid"; do
                if [ -n "$process_pid" ] && ! kill -0 "$process_pid" 2>/dev/null; then
                    wait "$process_pid" || true
                    return 0
                fi
            done
            sleep 1
        done
    }

    load_run_ports
    save_run_ports
    echo "Saved run ports to $run_state_file"

    case "$target" in
        backend)
            assert_backend_extra
            run_backend
            ;;
        frontend)
            assert_pnpm_installed
            trap cleanup_processes EXIT INT TERM
            run_frontend_admin &
            frontend_admin_pid=$!
            run_frontend_public &
            frontend_public_pid=$!
            wait_for_first_exit
            ;;
        all)
            assert_backend_extra
            assert_pnpm_installed
            trap cleanup_processes EXIT INT TERM
            run_backend &
            backend_pid=$!
            run_frontend_admin &
            frontend_admin_pid=$!
            run_frontend_public &
            frontend_public_pid=$!
            wait_for_first_exit
            ;;
        docker)
            echo "Starting services with Docker Compose..."
            if [ ! -f ".env.local" ]; then
                echo ".env.local is required for 'just run docker'. Copy .env.example to"
                echo ".env.local and set your own service addresses (DATABASE_URL, S3_*, ...)."
                exit 1
            fi
            # Containers cannot reach the host via localhost/127.0.0.1; only the
            # backend-facing DATABASE_URL / S3_ENDPOINT need host.docker.internal.
            # Generate .env.local.docker from .env.local on first run, then keep
            # the generated file so users can tweak it manually without being overwritten.
            compose_env_file=".env.local.docker"
            if [ -f "$compose_env_file" ]; then
                echo "Using existing $compose_env_file (delete it to regenerate from .env.local)"
            else
                sed -E \
                    -e '/^(DATABASE_URL|S3_ENDPOINT)=/ s#(@|//)(localhost|127\.0\.0\.1)#\1host.docker.internal#g' \
                    .env.local > "$compose_env_file"
                echo "Generated $compose_env_file from .env.local (localhost -> host.docker.internal for DATABASE_URL/S3_ENDPOINT)"
            fi
            # Layer env like settings.py: load .env first, then .env.local.docker overrides it.
            env_file_args=()
            [ -f ".env" ] && env_file_args+=(--env-file .env)
            env_file_args+=(--env-file "$compose_env_file")
            COMPOSE_LOCAL_ENV_FILE="$compose_env_file" docker compose "${env_file_args[@]}" up --build
            ;;
        *)
            echo "ERROR: Unknown run target: $target"
            echo "Usage: just run [backend|frontend|all|docker]"
            exit 1
            ;;
    esac

# Stop local development services by remembered or provided ports.
# Usage:
#   just down                        # stop backend + frontend-admin + frontend-public
#   just down backend                # stop backend only
#   just down frontend               # stop frontend-admin + frontend-public
#   just down backend backend_port=8010 frontend_admin_port=5179 frontend_public_port=3001
down arg1="" arg2="" arg3="" arg4="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    backend_port=""
    frontend_admin_port=""
    frontend_public_port=""
    run_state_file="{{justfile_directory()}}/.env.run-state"

    parse_down_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then return 0; fi

        case "$cli_arg" in
            backend|frontend|all|docker)
                target="$cli_arg"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_admin_port=*)
                frontend_admin_port="${cli_arg#frontend_admin_port=}"
                ;;
            frontend_public_port=*)
                frontend_public_port="${cli_arg#frontend_public_port=}"
                ;;
            *)
                echo "ERROR: Unexpected down argument: $cli_arg"
                echo "Usage: just down [backend|frontend|all|docker] [backend_port=<p>]"
                echo "                            [frontend_admin_port=<p>] [frontend_public_port=<p>]"
                exit 1
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}} {{quote(arg4)}}; do
        parse_down_arg "$cli_arg"
    done

    load_down_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi
        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_admin_port="${frontend_admin_port:-${FRONTEND_ADMIN_PORT:-5173}}"
        frontend_public_port="${frontend_public_port:-${FRONTEND_PUBLIC_PORT:-3000}}"
    }

    stop_port() {
        port_label="$1"
        port_value="$2"
        # Exclude Docker Desktop / dockerd processes so just down does not kill the Docker daemon
        process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"

        if [ -z "$process_ids" ]; then
            echo "No $port_label process listening on port $port_value"
            return 0
        fi

        echo "Stopping $port_label process(es) on port $port_value: $process_ids"
        kill $process_ids 2>/dev/null || true
        sleep 1

        remaining_process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"
        if [ -n "$remaining_process_ids" ]; then
            echo "Force stopping $port_label process(es) on port $port_value: $remaining_process_ids"
            kill -9 $remaining_process_ids 2>/dev/null || true
        fi
    }

    load_down_ports

    case "$target" in
        backend)
            stop_port backend "$backend_port"
            ;;
        frontend)
            stop_port frontend-admin "$frontend_admin_port"
            stop_port frontend-public "$frontend_public_port"
            ;;
        all)
            stop_port backend "$backend_port"
            stop_port frontend-admin "$frontend_admin_port"
            stop_port frontend-public "$frontend_public_port"
            ;;
        docker)
            docker compose down
            ;;
        *)
            echo "ERROR: Unknown down target: $target"
            echo "Usage: just down [backend|frontend|all|docker]"
            exit 1
            ;;
    esac


# ── Frontend ──────────────────────────────────────────────────────────────────

# Frontend helper
# Usage:
#   just frontend dev
#   just frontend build
#   just frontend install
frontend action="dev":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/frontend"
    case "{{action}}" in
        dev)
            npm run dev
            ;;
        build)
            npm run build
            ;;
        install)
            npm install
            ;;
        *)
            echo "ERROR: Unknown action: {{action}}"
            echo "Usage: just frontend [dev|build|install]"
            exit 1
            ;;
    esac


# ── Local Testing Middleware ──────────────────────────────────────────────────

# Manage the local testing middleware stack (docker-compose.testing.yml).
# Usage:
#   just testing                     # show running services (no side effects)
#   just testing up                  # apply compose changes / start all
#   just testing up ragflow          # apply / start a single service
#   just testing restart ragflow     # restart a service without rebuilding
#   just testing recreate ragflow    # force-recreate (picks up new image)
#   just testing recreate            # force-recreate all services
#   just testing down                # stop and remove the stack
testing action="ps" service="":
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{justfile_directory()}}
    compose_file="docker-compose.testing.yml"

    case "{{action}}" in
        ps)
            docker compose -f "$compose_file" ps
            ;;
        up)
            if [ -n "{{service}}" ]; then
                echo "Starting '{{service}}' from $compose_file"
                docker compose -f "$compose_file" up -d "{{service}}"
            else
                echo "Starting all services from $compose_file"
                docker compose -f "$compose_file" up -d
            fi
            ;;
        restart)
            if [ -n "{{service}}" ]; then
                echo "Restarting '{{service}}'"
                docker compose -f "$compose_file" restart "{{service}}"
            else
                echo "Restarting all services"
                docker compose -f "$compose_file" restart
            fi
            ;;
        recreate)
            if [ -n "{{service}}" ]; then
                echo "Force-recreating '{{service}}'"
                docker compose -f "$compose_file" up -d --force-recreate "{{service}}"
            else
                echo "Force-recreating all services"
                docker compose -f "$compose_file" up -d --force-recreate
            fi
            ;;
        down)
            echo "Stopping and removing the $compose_file stack"
            docker compose -f "$compose_file" down
            ;;
        *)
            echo "ERROR: Unknown testing action: {{action}}"
            echo "Usage: just testing [ps|up|restart|recreate|down] [service]"
            exit 1
            ;;
    esac
