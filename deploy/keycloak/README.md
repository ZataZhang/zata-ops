# Keycloak single node deployment

This directory deploys Keycloak `26.8.2` with a dedicated PostgreSQL `17.11`
database. It is the shared identity provider (IdP) for the template-derived
applications: each application registers as an OIDC client in the business
realm, and keeps its own sessions and business permissions.

It is a single node (`KC_CACHE=local`) and does not provide high availability.
If Keycloak is down, applications cannot start new logins; sessions that
applications already issued keep working.

| File | Purpose |
| --- | --- |
| `compose.yaml` | Plain VPS with an existing Traefik on the external `traefik` network. |
| `compose.dokploy.yaml` | Host whose 80/443 is managed by Dokploy (`dokploy-network`). |
| `.env.example` | Variables shared by both compose files. No real values. |
| `realm/zata-realm.json` | Business realm, login policy, team groups. No application clients. |

The design notes and the per-application onboarding steps live in
`docs/guides/keycloak.md`.

## Network boundary

- `KC_PUBLIC_HOST` is a bare hostname such as `auth.example.com`. Its DNS
  record must point to the server. Keycloak uses `https://<KC_PUBLIC_HOST>`
  as its public URL; TLS terminates at Traefik.
- Traefik routes only `/realms/<KC_REALM>/` and `/resources/`. The master
  realm, `/admin/`, and the management port `9000` (`/health`, `/metrics`) are
  never routed. This follows the Keycloak reverse proxy guide.
- The admin console binds to `127.0.0.1:${KC_ADMIN_LOCAL_PORT:-18180}` and is
  reached through an SSH tunnel. `KC_HOSTNAME_ADMIN` points to that address.
- PostgreSQL binds to `127.0.0.1:${KC_DB_LOCAL_PORT:-15432}` for backups only.

## Credentials

| Variable | Meaning |
| --- | --- |
| `KC_DB_PASSWORD` | Password of the `keycloak` database user. |
| `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD` | Temporary master realm admin. Create a permanent admin, then delete this user. |

Generate each value independently and keep it in the protected remote `.env`
(or the Dokploy Environment panel) and a private credential store. Never add
the values to this repository.

```bash
openssl rand -hex 24   # KC_DB_PASSWORD
openssl rand -hex 24   # KC_BOOTSTRAP_ADMIN_PASSWORD
```

## Install on a plain VPS

The server needs Docker Compose, the external `traefik` network, and a Traefik
with the `web`/`websecure` entrypoints and the `letsencrypt` resolver (see
`deploy/vps-traefik/`).

```bash
cp .env.example .env
chmod 600 .env
# fill in the values, then:
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml logs --tail=100 keycloak
```

## Install on Dokploy

Create a Compose application with the Git provider (this repository) and set
Compose Path to `deploy/keycloak/compose.dokploy.yaml`. The Raw provider is not
supported: it ships a single file, so `realm/` would become an empty
directory. Do not enable Isolated Deployment. Put the variables from
`.env.example` into the Environment panel.

## First login and permanent admin

```bash
ssh -N -L 18180:127.0.0.1:18180 root@<server-public-ip>
```

Open `http://127.0.0.1:18180/admin/`, sign in with the bootstrap admin, create
a permanent admin user in the master realm with the `admin` role, sign in as
that user, and delete the bootstrap admin.

## Realm configuration

`--import-realm` imports `realm/*.json` only when the realm does not exist yet:
on later starts the log shows that the import was skipped. The file is
therefore the source for creating the realm from scratch, not a sync tool.
`${...}` placeholders in the file resolve from the container environment.

For a change to a running realm (new client, new team group, new role):

1. Change `realm/zata-realm.json` and review it in a pull request.
2. Apply the same change to the running realm with `kcadm.sh` inside the
   container, for example:

   ```bash
   docker exec -it <keycloak-container> /opt/keycloak/bin/kcadm.sh config credentials \
     --server http://127.0.0.1:8080 --realm master --user <admin>
   docker exec -it <keycloak-container> /opt/keycloak/bin/kcadm.sh create groups/<teams-group-id>/children \
     -r zata -s name=team-a
   ```

The realm file ships no application client. Register one client per
application and environment when that application integrates, with its real
callback URL; `docs/guides/keycloak.md` has the client template. Develop an
application's OIDC login against a local Keycloak started from this same
compose file, not against the production realm.

## Backup and restore

PostgreSQL holds every account. Back it up more often than application
databases and rehearse a restore before onboarding real users.

```bash
zata-ops db backup --project keycloak \
  --db-url "postgresql://keycloak:<password>@127.0.0.1:15432/keycloak" --dry-run
```

The command uploads to the S3 bucket configured in the zata-ops `.env`; check
the `--dry-run` preview (bucket, keys, retention) before running it for real.
Run it on the server, or locally through
`ssh -N -L 15432:127.0.0.1:15432 root@<server>`.

Restore rehearsal: restore the dump into a new volume (change `KC_DB_VOLUME`),
start the stack, and confirm that users and client logins still work. The
previous volume stays untouched.

## Upgrade

Read the Keycloak upgrading guide for every version between the current and
the target one, back up the database, change `KC_VERSION`, and run
`docker compose up -d`. Keycloak migrates its schema on start; keep the backup
until the new version has served logins.
