# Deployment artifacts (prepared, not deployed)

These files describe how to run LedgerVigil's existing architecture on one host or VM for **deployment validation**. Nothing
here has been deployed, no paid resource has been created, and the container images have **not been built or run** (no
container runtime was available where they were written). Production financial writes, BOOKS ledgers with real data,
production QuickBooks and every provider write stay disabled. ADR-027 also keeps the deployment SANDBOX-only until TOTP MFA
ships (see docs/release/COMPLETION_MATRIX.md).

| File | What |
|---|---|
| `compose.yaml` | PostgreSQL 18, Redis, migrator (one-shot), API, frontend, maintenance worker, QuickBooks provider worker (opt-in profile), scheduler, Caddy TLS proxy |
| `Dockerfile.api` | One image for API, workers, scheduler and migrator; each service runs as its own uid |
| `Dockerfile.web` | Next.js production server |
| `proxy/Caddyfile` | TLS, routing and redacted access logs (query strings with OAuth codes/state, cookies and authorization headers removed) |
| `env/*.example` | Which secret files each service needs; copy and fill on the target host, never commit |
| `RUNBOOK.md` | Pre-flight, start/stop, backups and restore, key rotation, incidents, rollback |

## Process identities and secrets

| Service | uid | Database login | Secrets mounted | Network |
|---|---|---|---|---|
| api | 10001 | `lv_app` | app DSN, cursor key, KEK **public** key, QuickBooks API config (no client secret) | backend, edge |
| web | 10002 | none | none | edge |
| maintenance | 10003 | `lv_worker` | worker DSN | backend, queue |
| provider-worker | 10004 | `lv_worker` | worker DSN, QuickBooks worker config, KEK **private** key | backend, queue, egress |
| scheduler | 10005 | `lv_worker` | worker DSN | backend, queue |
| migrate | 10006 | `lv_migrator` → `lv_owner` | migrator DSN | backend |

The database (`backend`) and Redis (`queue`) networks are internal; the API is not on `queue`; only the proxy publishes
ports. Every application container runs read-only with `no-new-privileges` and all capabilities dropped; the proxy keeps
only `NET_BIND_SERVICE`. Remaining hardening gaps: the frontend image still contains development dependencies (Next.js
loads `next.config.ts` at start-up), Redis has no password (network isolation only), and the Caddy image runs as root
inside its container. `tests/test_deployment_isolation.py` checks these rules in
the compose file, and `tests/test_provider_sandbox.py::test_runtime_role_cannot_reach_worker_material` checks that the API's
database login cannot read credentials or call worker functions.

Known limitation: the maintenance worker and the provider worker share the `lv_worker` database login. The maintenance worker
holds no QuickBooks key material, so provider ciphertext it could reach through worker functions cannot be opened by it. A
separate `lv_integration` login would narrow this further; it needs a role-provisioning change and is not done.

## Before you start

1. Pin every image by digest (`docker buildx imagetools inspect caddy:2.10-alpine`, `python:3.14-slim`, `node:22-slim`) and
   replace the tags in `compose.yaml` and the Dockerfiles. The PostgreSQL and Redis digests are the ones the development stack
   already uses.
2. Create a private secrets directory **outside the repository** on the target host (for example
   `/srv/ledgervigil-secrets`, mode 0700) and export `LV_SECRETS_DIR` to it before every `docker compose` command. Put one
   file per secret named in `compose.yaml` there. Generate passwords with `python3 -c "import secrets;print(secrets.token_hex(32))"`. Make each file readable
   only by the uid of the service that mounts it (`chown 10004 "$LV_SECRETS_DIR"/provider/worker/*; chmod 0400 ...`).
3. QuickBooks sandbox only (optional): `python -m tools.provider_setup --container "$LV_SECRETS_DIR/provider"` (hidden prompts;
   never paste values into chat), then start with `--profile provider-sandbox` and set `LV_PROVIDER_SANDBOX_ENABLED=1` on
   `api` and `scheduler`.
4. Choose where trusted anchors live **off this host** (deploy/RUNBOOK.md §Anchors). Local Git is not offsite storage and not
   a signature.

## First start

```sh
cd deploy
docker compose up -d db broker
docker compose run --rm -e LV_SUPERUSER_DATABASE_URL_FILE=/run/secrets/superuser_dsn -e LV_ROLE_PASSWORD_DIR=/run/secrets/roles migrate python -m tools.deploy_entry prepare
docker compose --profile migrate run --rm migrate
docker compose up -d api web maintenance scheduler proxy
docker compose exec api python -m tools.deploy_entry readiness
```

`prepare` needs two extra secret mounts (superuser DSN and a directory with one password file per role) for that single run;
add them with `-v` or a temporary override file and remove them afterwards. `readiness` prints only check names and booleans.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `LV_ENVIRONMENT` | api | `production` (default) or `development` (allows local-http cookies on loopback only) |
| `LV_COOKIE_MODE` | api | `secure` (default, `__Host-` cookie) or `local-http` (development only) |
| `LV_ALLOWED_ORIGINS` | api | Comma-separated exact origins allowed for unsafe requests |
| `LV_CURSOR_KEY` / `_FILE` | api | ≥64 hex characters; required outside development so list cursors survive restarts |
| `LV_DATABASE_URL` / `_FILE` | api | `lv_app` DSN |
| `LV_WORKER_DATABASE_URL` / `_FILE` | workers, scheduler | `lv_worker` DSN |
| `LV_MIGRATOR_DATABASE_URL_FILE` | migrate, readiness | `lv_migrator` DSN |
| `LV_MAINTENANCE_BROKER_URL` | workers, scheduler | Redis URL |
| `LV_EXPORT_QUEUE`, `LV_PROVIDER_QUEUE` | workers | Queue names (defaults `native-exports`, `integration`) |
| `LV_ANCHOR_REPOSITORY` | api, maintenance | Trusted-anchor working copy |
| `LV_DETECTION_ENABLED` | api | `1` enables the advisory detection slice (default off) |
| `LV_PROVIDER_SANDBOX_ENABLED` | api, scheduler | `1` enables the QuickBooks sandbox connection (default off) |
| `LV_PROVIDER_API_CONFIG` | api | QuickBooks API config file (client id, redirect URI, KEK public key path) |
| `LV_PROVIDER_WORKER_CONFIG` | provider-worker | QuickBooks worker config (adds client secret and KEK private key path) |
| `LV_PROVIDER_KEK_PRIVATE_PREVIOUS` | provider-worker (in its config) | Old private keys kept during a key rotation |
| `LV_FORWARDED_ALLOW_IPS` | api | Proxy addresses trusted for `X-Forwarded-*` |
| `LV_API_ORIGIN` | web | Where the frontend forwards `/api/v1` (default `http://127.0.0.1:8000`) |


### Identity issuer (0038 candidate)

Provision a distinct restricted lv_authn login and private issuer_dsn. The issuer
service receives this DSN and no application or provider credential; the API
receives app_dsn and no issuer DSN. The TLS proxy sends /api/v1/auth/* and
/api/v1/members* to the issuer; other API paths go to the API service.
The gateway is the only component on both edge networks. Neither issuer port
nor database port is published. If configuring SMTP, put its delivery secret
on the issuer only. Real mail delivery has not been verified.

For operator readiness, provide all four database DSNs (application, worker,
issuer, migrator) and the anchor repository. Never mount operator credentials
into the runtime services. Local combined mode uses two database roles under
one Mac OS process and is not an OS isolation boundary.
