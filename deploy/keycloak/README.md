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
| `realm/realm.json` | Business realm (name from `KC_REALM`), shared login flow, email settings and team groups. |
| `realm/clients/freshai-user.json` | FreshAI user client template with verified phone claim mappers. |
| `providers/shared-otp-authenticator/` | Versioned password/email-code/SMS-code authenticator and neutral shared login theme. |

The provider bundles the Alibaba SMS SDK dependencies but excludes Bouncy Castle;
Keycloak supplies its own crypto libraries, avoiding duplicate classes at startup.

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
| `KC_SHARED_OTP_HMAC_SECRET` | Stable random HMAC key used to protect OTP records and rate-limit keys. |
| `KC_OTP_SMS_*` | Alibaba Cloud Dypnsapi sender for mainland China SMS verification codes. |

Generate each value independently and keep it in the protected remote `.env`
(or the Dokploy Environment panel) and a private credential store. Never add
the values to this repository.

```bash
openssl rand -hex 24   # KC_DB_PASSWORD
openssl rand -hex 24   # KC_BOOTSTRAP_ADMIN_PASSWORD
openssl rand -hex 32   # KC_SHARED_OTP_HMAC_SECRET
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

The master realm is not routed publicly, but by default its URLs use the
public `KC_HOSTNAME`, so the admin console times out with "Timeout when
waiting for 3rd party check iframe message". Once after the first start,
point the master realm's frontend URL at the tunnel address:

```bash
docker exec <keycloak-container> bash -c '
K=/opt/keycloak/bin/kcadm.sh
$K config credentials --server http://127.0.0.1:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" --config /tmp/k.cfg
$K update realms/master -s attributes.frontendUrl=http://127.0.0.1:18180 --config /tmp/k.cfg
rm -f /tmp/k.cfg'
```

The setting lives in the database and only affects the master realm; the
business realm keeps the public issuer. Use the same port as
`KC_ADMIN_LOCAL_PORT`.

```bash
ssh -N -L 18180:127.0.0.1:18180 root@<server-public-ip>
```

Open `http://127.0.0.1:18180/admin/`, sign in with the bootstrap admin, create
a permanent admin user in the master realm with the `admin` role, sign in as
that user, and delete the bootstrap admin.

## Realm configuration

Keycloak requires an import file to be named `<realm>-realm.json`, so the
compose files mount `realm/realm.json` as `${KC_REALM}-realm.json`.
`--import-realm` imports it only when the realm does not exist yet:
on later starts the log shows that the import was skipped. The file is
therefore the source for creating the realm from scratch, not a sync tool.
`${...}` placeholders in the file resolve from the container environment at
import time only; an unset variable stays literal. Keycloak restricts realm
placeholders to names in `KC_SPI_ADMIN__ALLOWED_SYSTEM_VARIABLES`; both compose
files allow only the realm name and display name.

SMTP is not part of the file or the container environment. A running realm
does not resolve `${...}` in its SMTP settings, and the Admin API rejects them
for the sender and reply-to addresses. After the first start, configure Realm
settings → Email once with literal values (bare addresses in `from` and
`replyTo`, the display name in its own field, `auth` set to `true`). The
password is stored in the Keycloak database and masked by the Admin API; treat
database backups accordingly. Changing mail settings later happens in the same
place, not in the deployment environment.

For a change to a running realm (new client, new team group, new role):

1. Change `realm/realm.json` and review it in a pull request.
2. Apply the same change to the running realm with `kcadm.sh` inside the
   container, for example:

   ```bash
   docker exec -it <keycloak-container> /opt/keycloak/bin/kcadm.sh config credentials \
     --server http://127.0.0.1:8080 --realm master --user <admin>
   docker exec -it <keycloak-container> /opt/keycloak/bin/kcadm.sh create groups/<teams-group-id>/children \
     -r <realm> -s name=team-a
   ```

The shared OTP flow creates a user with only the contact that was verified.
Apply `realm/user-profile.json` so email, first name and last name remain
optional; this lets phone-only users finish their first login without an
unrelated profile-completion screen. It also declares the `phone_number` and
`phone_number_verified` attributes written by SMS sign-in, so administrators
can see them on the user details page; users cannot view or edit them.
SMS sign-in names a new user by the E.164 number (for example
`+8613800000000`) and email sign-in by the address, so the admin user search
matches number fragments such as `*13800*`. The username reaches every client
as `preferred_username`. Copy the file into the Keycloak
container and update the realm profile:

```bash
docker cp deploy/keycloak/realm/user-profile.json \
  <keycloak-container>:/tmp/shared-user-profile.json
docker exec <keycloak-container> /opt/keycloak/bin/kcadm.sh update users/profile \
  -r <realm> -f /tmp/shared-user-profile.json
```

The realm file ships no live application client. Register one confidential
client per application and environment with its exact callback URL; the
`realm/clients/freshai-user.json` file is the FreshAI user-client template.
Develop an application's OIDC login against a local Keycloak started from
this same compose file, not against the production realm.

The realm configures `shared-browser` as the browser flow and `shared` as the
neutral login theme. Its first alternative checks the Keycloak SSO cookie; the
custom authenticator is the next alternative for password, email-code and
SMS-code sign-in. That order lets an existing Realm session skip credential
entry for another client. The authenticator creates a Keycloak user after OTP
proof when realm self-registration is enabled; each application separately
checks its own registration policy before creating a local account. OTP attempts
and per-IP code requests reserve expiring slots atomically, so concurrent
requests cannot bypass their limits. SMTP settings stay in the Keycloak
database and Aliyun SMS credentials in the Keycloak container; neither is copied
to app services. A failed code delivery is logged with the channel and
exception type only, without the address or phone number, and releases the
resend lock so the user can retry at once. Email-code sign-in only matches an account
whose email is verified, so an address someone typed into another account
cannot be used to enter that account.

Realm import is skipped when the realm already exists. Updating
`realm/realm.json` therefore does not change an existing `dewell` realm.
After building and deploying the custom image, apply the shared flow, login
theme, optional user profile, registration/email verification and SMTP settings
through the Keycloak Admin Console or `kcadm.sh` command above. Review the
change before setting Browser Flow to
`shared-browser`; that binding affects every client in the realm. Keep a
recovery admin session open while applying it.

For FreshAI, copy `realm/clients/freshai-user.json`, replace the example
Home URL (`baseUrl`, used for the terms and privacy links of the `freshai`
login theme) and redirect URI with the callback for that environment, and apply it to the
`freshai` confidential client. Do not put the generated client secret in this
repository. Local callback testing also requires the local callback to be
explicitly added to the client's redirect allowlist. Keep `freshai-admin` as
a separate client.

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

## Upgrade an existing realm with one configuration command

Deploy the custom image first, keeping the existing database volume. On the Docker
host, run the following from the zata-ops checkout. The password is prompted by
Keycloak; it is not placed in the command or shell history:

```bash
docker exec -it <keycloak-container> /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://127.0.0.1:8080 --realm master --user <admin> \
  --config /tmp/shared-login-kcadm.config
python3 scripts/keycloak/upgrade_shared_login.py \
  --container <keycloak-container> --realm <realm> \
  --snapshot /secure/path/shared-login-before.json
```

The default mode only reads server configuration and prints a plan. Add `--apply`
to create the two-step shared flow, select the theme, enable self-registration,
and make email/first/last name optional while preserving other profile attributes.
The Provider and theme must already be installed; a conflicting existing flow is
rejected. The snapshot is created exclusively with mode 0600 before changes;
choose a new path per run. Repeating the upgrade is supported. Keep an admin
session open because the browser binding affects every client in the Realm.

If an apply step fails, the command attempts to restore the original realm
settings and profile. To restore explicitly, use the original snapshot:

```bash
python3 scripts/keycloak/upgrade_shared_login.py \
  --container <keycloak-container> --realm <realm> \
  --snapshot /secure/path/shared-login-before.json --rollback --apply
```

Rollback restores bindings/profile; it leaves the newly created flow available
but unbound. SMTP/SMS credentials, SMTP Realm configuration, clients and users
are not changed by this command. Configure mail delivery in Realm Settings →
Email and the SMS transport in the Keycloak container environment before testing
registration. Image deployment and provider installation remain the Compose
application's responsibility. Remove the temporary Admin CLI credential file
inside the container after use; it contains an admin session token.
