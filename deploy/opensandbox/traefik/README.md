# Optional public HTTPS ingress through Traefik

This profile routes a chosen hostname through an existing Traefik v3 instance. The OpenSandbox API stays bound to `127.0.0.1:18080`; port `18080` and sandbox ports `41000–41100` are not published publicly. A dedicated Docker internal network contains only Traefik, and a systemd socket proxy listens only on that network's host gateway.

The ingress still exposes the OpenSandbox lifecycle API to the internet. The API key is an administrator credential for a host-level service: use it only from a trusted backend, never from a browser, model prompt, or sandbox. This is suitable for a single trusted operator, not an untrusted multi-tenant product.

## Target requirements

- OpenSandbox is installed and healthy with API-key authentication enabled.
- Traefik v3 is already running with the Docker network provider or file provider, entrypoints named `web` / `websecure`, and a certificate resolver named `letsencrypt` using HTTP-01.
- Public DNS for the chosen hostname points to Traefik, and inbound TCP 80/443 already reach Traefik.
- `systemd-socket-proxyd` is present at `/usr/lib/systemd/systemd-socket-proxyd` or `/lib/systemd/systemd-socket-proxyd`.

## Install

Run as root on the target. Replace `sandbox.example.com` with the public hostname. First create the isolated bridge and attach Traefik:

```bash
docker network create --driver bridge --internal opensandbox-ingress
docker network connect opensandbox-ingress traefik
docker network inspect opensandbox-ingress --format '{{(index .IPAM.Config 0).Gateway}}'
```

Add the network from `compose-network.snippet.yml` to the Traefik Compose definition so a later Traefik recreation keeps the attachment. Validate the Compose file without recreating the running container:

```bash
docker compose -f /opt/traefik/docker-compose.yml config --quiet
```

Use the gateway returned by `docker network inspect` to render the templates below. Replace `@INGRESS_GATEWAY@` with that gateway and `@DOMAIN@` with the public hostname:

```bash
gateway="$(docker network inspect opensandbox-ingress --format '{{(index .IPAM.Config 0).Gateway}}')"
domain="sandbox.example.com"
sed "s/@INGRESS_GATEWAY@/${gateway}/g" opensandbox-proxy.socket.in > /tmp/opensandbox-proxy.socket
sed -e "s/@INGRESS_GATEWAY@/${gateway}/g" -e "s/@DOMAIN@/${domain}/g" \
  opensandbox-dynamic.yml.in > /tmp/opensandbox-dynamic.yml
install -m 0644 opensandbox-proxy.service /etc/systemd/system/opensandbox-proxy.service
install -m 0644 /tmp/opensandbox-proxy.socket /etc/systemd/system/opensandbox-proxy.socket
install -m 0644 /tmp/opensandbox-dynamic.yml /opt/traefik/dynamic/opensandbox.yml
systemctl daemon-reload
systemctl enable --now opensandbox-proxy.socket
```

Back up any existing file before replacing it. Traefik's file provider watches the dynamic directory, so adding the route does not require a Traefik restart. The selected resolver obtains the hostname certificate through its configured HTTP-01 challenge.

## Verify

The private relay should return `401` without the API key. That proves it reaches the authenticated API without exposing the key:

```bash
gateway="$(docker network inspect opensandbox-ingress --format '{{(index .IPAM.Config 0).Gateway}}')"
curl -i "http://${gateway}:18081/"
curl -i "http://sandbox.example.com/"
curl -i "https://sandbox.example.com/v1/sandboxes"
```

The HTTP request should redirect to HTTPS. The unauthenticated API request should return `401`; an authenticated client can then use the normal OpenSandbox SDK with `OPENSANDBOX_BASE_URL=https://sandbox.example.com` and `OPENSANDBOX_API_KEY` injected by a trusted secret manager.

## Roll back

Remove `/opt/traefik/dynamic/opensandbox.yml`, then stop and disable the socket before deleting its unit files:

```bash
systemctl disable --now opensandbox-proxy.socket
rm -f /opt/traefik/dynamic/opensandbox.yml
rm -f /etc/systemd/system/opensandbox-proxy.socket /etc/systemd/system/opensandbox-proxy.service
systemctl daemon-reload
```

Keep the internal Docker network if another service uses it. Otherwise disconnect Traefik and remove the network after confirming it is empty. Never remove or publish the OpenSandbox API or sandbox port ranges as part of rollback.
