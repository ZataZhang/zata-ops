#!/bin/sh
set -eu

config=${1:?Usage: zata-ops-audit-worker CONFIG}
interval=${ZATA_OPS_AUDIT_INTERVAL_SECONDS:-30}
case "$interval" in
    ''|*[!0-9]*|0|0*) echo "Invalid audit interval" >&2; exit 2 ;;
esac

trap 'exit 0' TERM INT
while :; do
    if zata-ops audit archive --config "$config"; then
        :
    else
        echo '{"event":"audit_worker_retry"}' >&2
    fi
    sleep "$interval" &
    wait "$!"
done
