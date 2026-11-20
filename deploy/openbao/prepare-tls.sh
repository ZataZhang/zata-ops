#!/bin/sh
set -eu

cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root on the Linux Docker host." >&2
    exit 1
fi
if [ -e tls ] || [ -L tls ]; then
    echo "tls already exists. Use a separate directory to stage certificate rotation." >&2
    exit 1
fi

umask 077
staging_dir=$(mktemp -d .openbao-tls.XXXXXXXX)
trap 'rm -f "$staging_dir/server.crt" "$staging_dir/server.key"; rmdir "$staging_dir"' EXIT

openssl req -new -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -sha256 -days 365 -subj '/CN=localhost' \
    -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature' \
    -addext 'extendedKeyUsage=serverAuth' \
    -keyout "$staging_dir/server.key" -out "$staging_dir/server.crt"

# The pinned Alpine image's openbao group is GID 1000.
chgrp 1000 "$staging_dir" "$staging_dir/server.key"
chmod 0750 "$staging_dir"
chmod 0640 "$staging_dir/server.key"
chmod 0644 "$staging_dir/server.crt"
mv -T -n "$staging_dir" tls
if [ -d "$staging_dir" ]; then
    echo "tls was created by another process." >&2
    exit 1
fi
trap - EXIT
