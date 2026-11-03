# Nacos single node deployment

This Compose project runs Nacos `v3.2.4` in standalone mode with the embedded
Derby database. It is intended for development, test, and low-risk internal
use. It is a single node and does not provide high availability.

Nacos 3.x is not compatible with the Nacos 1.x and 2.x HTTP OpenAPI. Confirm
that each client SDK supports Nacos 3.x before connecting it to this instance.

## Network boundary

- Set `NACOS_PUBLIC_HOST` to a DNS name whose A record points to the server.
  Traefik serves the Console at `https://<public-host>/` and routes the Nacos
  3.x client HTTP API under `https://<public-host>/nacos/` with HTTPS.
- The Traefik container and Nacos container share the external Docker network
  named `traefik`. Traefik uses the existing `websecure` entrypoint and the
  `letsencrypt` certificate resolver; HTTP requests redirect to HTTPS.
- Set `NACOS_BIND_ADDRESS` to the server's private interface address. Ports
  `8848` (HTTP API) and `9848` (client gRPC) bind only to that address.
- The Console also binds to `127.0.0.1:18090` for SSH-tunnel administration.
- Restrict access to the private API and gRPC ports with the cloud security
  group to the client networks that need Nacos.
- Port `9848` is gRPC and must not be routed as HTTP.
- Public Nacos SDK clients need a separate gRPC path on `9848`; this template
  keeps gRPC on the private interface.

## Credential meanings

- The Console administrator username is `nacos`. Set its password in the
  first-use Console page; it is not stored in the Compose `.env` file.
- `NACOS_AUTH_TOKEN` is the Base64 JWT-signing secret. Its decoded value must
  contain at least 32 bytes.
- `NACOS_AUTH_IDENTITY_KEY` and `NACOS_AUTH_IDENTITY_VALUE` form the server
  identity pair used by Nacos internal APIs. Keep both deployment-specific.
- Store the three server startup values in the protected remote `.env` file
  and a local private credential store. Never add their values to this repo.

## Install

Copy `.env.example` to `.env`, set the public hostname and private interface
address, choose the persistent data volume name, and generate three
deployment-specific authentication values. The token must be Base64 that
decodes to at least 32 bytes. Generate the identity key and value independently.
Do not commit `.env` or copy credentials from another Nacos deployment.

```bash
cp .env.example .env
chmod 600 .env
openssl rand -base64 48
openssl rand -hex 32
openssl rand -hex 32
```

Fill the generated values into `.env`, then validate and start the service:

```bash
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml logs --tail=100 nacos
```

Initialize the `nacos` administrator password on first Console access. Store
it in a private password manager. Enter a password in the initialization page;
leaving it blank makes Nacos generate a random password. The data volume named
by `NACOS_DATA_VOLUME` survives container recreation; create and test separate
backups before treating the instance as a source of important configuration.

To start with empty Nacos data while preserving the old data, change
`NACOS_DATA_VOLUME` to a new name and run `docker compose up -d`. Compose creates
the new volume and leaves the old one in place. Switching the variable back
selects the old data; no data is migrated between volumes.

From a workstation, create the SSH tunnel and open the local URL:

```bash
ssh -N -L 18090:127.0.0.1:18090 root@<server-public-ip>
```

Open `http://127.0.0.1:18090/`. Clients in the private network use
`http://<server-private-ip>:8848` and gRPC at `<server-private-ip>:9848`.
Public clients that support the Nacos 3.x HTTP API can use
`https://<public-host>/nacos/`.

## Stop and remove

Stop the container while retaining its data:

```bash
docker compose --env-file .env -f compose.yaml down
```

Removing the volume deletes Nacos configuration and registry data. Only do so
when an intentional reset has been confirmed:

```bash
docker compose --env-file .env -f compose.yaml down --volumes
```
