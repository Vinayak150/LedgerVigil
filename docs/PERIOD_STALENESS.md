# Native period-close staleness consumer

Migration 0014_period_staleness implements the existing STALENESS_SCAN/PERIOD queue
created by authenticated period close. Scope is BLOCKED and READY_FOR_REVIEW native
proposals with the changed period in their pinned read set. No approval, rejection,
override, execution, provider access or financial posting capability is added.
Accounting and oracles remain UNREVIEWED; all earlier proposed decisions stay proposed.

## Contract ownership

- Stage 2A §10.3–10.4: changed read-set versions make pending proposals STALE.
- Stage 2A §13.1–13.2 and ADR-010: PostgreSQL outbox, Celery/Redis transport,
  identifier-only messages, leases, fencing and a singleton scheduler.
- Stage 2B Family 4: period close atomically queues its staleness scan.
- Stage 2B staged-document lifecycle: STALE releases RESERVED statements to VALID.
- Stage 2A §10.4: maker withdrawal remains available from non-terminal STALE.

Rejection is contracted only from READY_FOR_REVIEW. Current synthetic cases remain
BLOCKED_INSUFFICIENT, so this milestone does not invent a rejection-from-BLOCKED path
or fabricate a ready case. General action-time revalidation remains mandatory for
future approval/execution; this consumer is a bounded asynchronous period-close path.

## Durable protocol

The relay selects due PENDING maintenance outbox rows with SKIP LOCKED, publishes
only tenant/job UUIDs, then marks DISPATCHED in the same PostgreSQL transaction.
A crash after publish rolls that transaction back, so publication repeats safely.
The broker stores no financial request or result. No Celery result backend is used.

Each task conditionally claims its work item, commits the claim, advances the
fencing token and holds a 60-second lease. Duplicate delivery cannot claim an owned
or finished item. Every step locks the work item and checks worker identity, token
and lease. Null identity/token values fail closed. One proposal is processed per
transaction in proposal → ledger lock order. Progress commits between proposals;
subsequent transactions continue from a UUID cursor, without redoing completed work.
A crashed task resumes at committed progress after lease expiry and re-claim.

The job is tied to an actual AUDITED_COMMIT period close: the ledger, period and
close sequence must match. Only proposals citing that exact period are considered.
A different current period version produces STALE with one resource-version advance,
an immutable system transition, audit event, maintenance event and complete release
of that proposal's statements in the same transaction. Revision contents, pinned
simulation, read set and financial data are retained. A commit-time validator
independently rejects a stale transition with missing claim proof, release or audit.
A lease that expires during tentative work cannot be revived by the progress update;
the transaction rolls back. A concurrent withdrawal yields one release.

The sweeper fences expired claims, re-queues retryable work with bounded exponential
backoff and re-publishes dispatched-but-unclaimed work after a 30-second watchdog.
That watchdog also recovers loss of Redis data. Five failed attempts produce durable
DEAD status, an immutable DEAD event and a MAINTENANCE_JOB_DEAD audit event. Scheduler
health logs report nonzero DEAD counts; `status` shows PostgreSQL job counts. This
is a local operational alert condition, not an external notification integration.

The scheduler holds a database advisory lock for its connection lifetime. A second
scheduler exits. If its database connection is lost, restart is required. Worker
statements have a 15-second timeout and a 3-second lock timeout. Redis visibility is
600 seconds, beyond the configured 300/330-second task limits. The local launcher
uses Celery's solo pool; hard/soft time-limit behavior of a production pool, load
capacity and watchdog tuning have not been established by the local tests.

## Restricted system identity

`lv_worker` is a separate NOINHERIT/NOSUPERUSER/NOBYPASSRLS login. It receives no
financial or identity table access and no source, seed, proposal or execution
entry points. It can execute only seven maintenance functions. Even internal
helpers are unavailable to that login directly; app/seed/ingest logins cannot call
the maintenance entry points. Function owners/search paths remain restricted/pinned.

All tenant tables retain FORCE RLS. Work/outbox have narrow owner policies conditioned
on the authenticated session_user being lv_worker, allowing global queue row locking
inside definer functions. Updates still check the selected tenant. Other table access
runs under that tenant context. Direct worker table reads/writes are denied. The new
maintenance-event table has FORCE RLS and immutable history.

System stale/release records have null human user/session columns plus a job ID and
fencing token. They never impersonate the maker. Existing human transition/release
rows are preserved and constrained to the human attribution shape. Worker credentials
are appended to the private mode-0600 local role file; existing credentials are not
rotated or included in a review package.

## Local operation

From the configured shared app, install pinned worker additions alongside the
existing base lock, prepare owned roles, and migrate (already done for this delivery):

```sh
.venv/bin/python -m pip install -r worker-requirements.lock
.venv/bin/python -m tools.dev_db prepare
.venv/bin/python -m tools.dev_db migrate
```

The optional Compose profile binds Redis only to 127.0.0.1:56379, with a pinned image
digest and its own volume. It does not start when only the existing db profile runs.
The following separate terminals enable local maintenance explicitly:

```sh
docker compose --profile maintenance up -d maintenance-redis
.venv/bin/python -m tools.run_maintenance worker
.venv/bin/python -m tools.run_maintenance scheduler
```

On this Mac, the Compose plugin is not discovered under the isolated Docker config.
The Homebrew standalone `/opt/homebrew/bin/docker-compose` can be used with the
existing Colima DOCKER_HOST and the project .docker-local config; its maintenance
profile was validated without printing resolved secrets. Global Docker settings
and credential stores were not changed.

Inspect durable status with `.venv/bin/python -m tools.run_maintenance status`.
`once` performs one sweep/relay pass, without claiming to be a persistent scheduler.
LV_MAINTENANCE_BROKER_URL can override the loopback default. Runtime modules require
LV_WORKER_DATABASE_URL; the local launcher constructs it from the private role file
in-process. Do not put DSNs in commands or logs. Production network/auth/TLS settings,
process supervision, monitoring delivery and credential rotation remain deployment
work; no public deployment is enabled by this slice.

## Limits

Only audited period-close jobs are consumed. Account/document configuration changes,
later source conflicts, snapshot eligibility and broad source/range dependencies
still require future action-time validation and additional queue producers/consumers.
No general background-staleness coverage is claimed. Explicit maker freshness
checks and unchanged-content re-simulation are now available under migration 0015;
see RESIMULATION.md. Human review/rejection remains a separate milestone.

The suite also exposed a pre-existing Base64 cursor alias: alternate text could decode
to the same signed bytes. Reads now require canonical Base64url encoding. A deterministic
256-key test verifies that aliases fail and genuine cursors still decode; existing
Unicode-safe cursor handling is preserved.

SDK references used for transport settings: [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
and [Redis transport](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html).
