# Background reconciliation: retries and recovery

PostgreSQL owns job identity, dispatch intent, attempt count, retry timing and
completion. Redis/RQ transports job UUIDs to a separate worker. A small separate
dispatcher scans durable job rows, resends due queued work and recovers expired
worker attempts. Run both the worker and dispatcher for recovery to make progress.

Delivery is **at least once while recovery processes and backends are available**,
with bounded execution attempts and terminal failure reporting. Redis messages can
be duplicated or lost and reconstructed from PostgreSQL. This is **not exactly-once
execution**. No automatic recovery can progress while PostgreSQL is unavailable,
or while all dispatcher/worker processes are stopped.

The synchronous reconciliation endpoint, imports, review services, request-key
contracts and matching rules remain compatible. This is still the single-user
demo: its bearer token grants access to all accounts, without multi-user ownership.
Background imports, cancellation, manual retries, dashboard and deployment remain
deferred.

## Setup and demos

Stop old worker/API processes before applying migration `0006_job_recovery`; old
workers do not understand attempt fencing and must not run against the new state.
Then, from the repository root:

```sh
make setup
docker compose up -d --wait postgres redis
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
export REDIS_URL='redis://localhost:56379/0'
make migrate
export LEDGER_DEMO_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LEDGER_DEMO_IDENTITY='local-demo-reviewer'
make api
```

Keep the API terminal open. In two additional terminals, export the same
`DATABASE_URL` and `REDIS_URL`, and run one command per terminal:

```sh
make worker
make dispatcher
```

In another terminal, export the same bearer token and run `make demo-jobs` for
HTTP submit → job ID → poll → succeed. It reuses sample records on sequential
reruns and uses a new key for each intended run. Unchanged reruns write no new
decisions. `LEDGER_API_URL` defaults to `http://127.0.0.1:8000`.

For a controlled temporary-failure demonstration, only PostgreSQL and Redis need
to be running; no pre-existing API/worker/dispatcher is required:

```sh
# DATABASE_URL must permit creating and dropping a private demo schema.
make demo-job-recovery
```

This script creates an isolated schema and queue, starts private API/worker/
dispatcher processes, and installs a private SQL trigger that raises SQLSTATE
`40001` once. It prints attempt 1's safe error and persisted retry time, waits for
the real backoff, and observes attempt 2 succeed. It verifies one reconciliation
decision and one reconciliation audit event, deliberately redelivers the completed
job, and checks that neither changed. Processes, schema and queue are cleaned up.
Failure injection exists only in this script and tests, never in public requests
or production worker configuration.

| Configuration | Purpose |
| --- | --- |
| `DATABASE_URL` | API, worker and dispatcher must share the same migrated database/schema |
| `REDIS_URL` | Shared Redis connection; Compose Redis 7 binds to localhost **56379** |
| `LEDGER_QUEUE_NAME` | Shared queue name, default `ledger-reconciliation` |
| `LEDGER_DEMO_TOKEN`, `LEDGER_DEMO_IDENTITY` | API authentication; worker/dispatcher do not need the bearer secret |
| `TEST_DATABASE_URL`, `TEST_REDIS_URL` | Explicit integration-test backends |

The demo RQ `SimpleWorker` runs one job at a time in a separate Python process,
with JSON serialization and a 300-second task timeout. RQ retries and its
scheduler are not enabled: PostgreSQL schedules all retries. The dispatcher
sweeps once per second, processes at most 100 rows per sweep, and supports
`python -m ledger.jobs.dispatcher --once` for a bounded sweep. Multiple
dispatchers can run concurrently using `FOR UPDATE SKIP LOCKED`.

## Existing HTTP contracts and additional status fields

Both endpoints retain bearer authentication and Swagger/OpenAPI declarations:

| Endpoint | Contract |
| --- | --- |
| `POST /accounts/{account_id}/reconciliation-jobs` | `{ "request_key": "caller-stable-key" }` → **202** with job record and `Location` status URL |
| `GET /accounts/{account_id}/reconciliation-jobs/{job_id}` | **200** with PostgreSQL status, or **404** for missing/wrong-account records |

Keys remain globally unique, trimmed, nonblank, NUL-free strings of at most 255
characters. The canonical payload contains account UUID, operation and version.
Identical replays return the same job's current status, including terminal jobs;
conflicting account reuse returns **409** `identity_conflict`. A replay does not
reset retry budgets, schedules or terminal outcomes. Unknown fields return **422**.
New jobs without Redis configuration still return **503** without retaining a
row; existing-key replay, reads and synchronous routes work without Redis.
Configured Redis outages now retain the accepted job as queued for recovery.

Existing fields remain: `id`, `account_id`, `request_key`, `status`, `attempt_count`,
`created_at`, `updated_at`, `started_at`, `finished_at`, `result_summary`, and
`failure_details`. Added fields are:

- `max_attempts`: persisted execution budget, currently three total attempts.
- `next_attempt_at`: earliest allowed claim time; null during running/terminal
  states. Retries keep the existing `queued` status instead of adding a new enum.
- `dispatch_after`: next eligible dispatch/resend time; null outside queued state.
- `lease_expires_at`: deadline for recovering an unlocked running attempt.
- `failure_details.retryable`: whether that diagnostic schedules further work.

`started_at` refers to the latest claimed attempt; `finished_at` is set only at a
terminal outcome. Failure details are fixed safe messages/codes, retained during
queued/running recovery and cleared on success. Internal attempt UUID tokens are
not exposed. Success summary fields remain identical to synchronous reconciliation:
`matched`, `needs_review`, `unmatched`, `unmatched_transactions`, `decisions_written`.

## Retry policy and failure classification

A claimed execution consumes one of three attempts. Transient execution failures
retry after **5 seconds**, then **10 seconds**. Backoff calculation is exponential
with a 60-second cap. Early duplicate delivery cannot bypass `next_attempt_at`.
After the third transient failure/interruption, status becomes `failed` with
`retry_exhausted`; no further automatic work is scheduled.

Transient cases are an explicit allowlist: database connection loss, pool timeout,
serialization failure (`40001`), deadlock (`40P01`), lock timeout (`55P03`), query
cancellation/timeout (`57014`), capacity/startup/shutdown errors, and RQ task timeout.
Validation, integrity constraints, unexpected errors and other database SQLSTATEs
are permanent and stop immediately with `reconciliation_failed`. Raw SQL,
credentials, input data and exception text are never included in status details.

Transient Redis connection/timeout failures leave durable queued work with
`dispatch_failed`; the dispatcher checks it again after 30 seconds. Dispatch and
lost-message recovery do **not** consume execution attempts: they continue while
infrastructure is unavailable and no worker has claimed the job. Authentication,
authorization, serialization and other non-transient dispatch errors stop with a
terminal safe `dispatch_failed` diagnostic. Terminal jobs require a new intended
request key for another run; there is no manual retry endpoint.

## Dispatch and transaction boundaries

1. **Acceptance:** the API commits the job and its dispatch timing together in one
   PostgreSQL transaction. The job row itself is the small durable outbox; no
   separate queue-state database or cross-system transaction is required.
2. **Dispatch:** the API optionally sends immediately. The dispatcher also selects
   due queued rows with `SKIP LOCKED`, enqueues a job UUID using a fresh RQ transport
   ID, and advances the resend deadline in a short row-locking transaction. If the
   process dies before/after enqueue or loses its acknowledgement, the durable row
   remains eligible for redelivery. Successfully enqueued work is resent every
   30 seconds until claimed, covering Redis restarts/message loss too.
3. **Claim:** a worker locks a due queued row, increments its attempt count, assigns
   a fresh attempt UUID, records a **360-second lease**, and commits `running`.
   Duplicate delivery cannot claim running, exhausted or terminal work.
4. **Execution and completion:** a second transaction locks the running job row
   and validates its attempt UUID and initial lease before any domain writes.
   The existing reconciliation core uses that same connection and account lock.
   Decisions, audit events, summary and `succeeded` status commit **together**.
   Synchronous callers still use the original engine-owned transaction wrapper.
5. **Failure:** the entire execution transaction rolls back. A separate fenced
   update schedules a retry or records terminal failure. If this update cannot
   commit, the durable running lease is later recovered by the dispatcher.

The worker sets a 300-second statement timeout during reconciliation. A running
transaction holds the job row lock throughout domain execution, so recovery skips
it even when its lease has expired. Once a killed/disconnected worker's database
transaction ends, the dispatcher can reclaim the expired lease. Every recovery or
completion checks the attempt UUID; a paused old attempt cannot execute after a
new claim or overwrite its result. PostgreSQL also resolves ambiguous commits:
either domain writes and success both committed, or neither did.

Expiry schedules the same bounded backoff with `worker_interrupted`, consuming
the already-claimed attempt. There is no heartbeat thread: row locking protects
active execution; the lease covers the committed-claim/execution gap. Recovery may
wait for PostgreSQL to detect disconnected sessions or release an active database
transaction. It never forcibly steals a live transaction.

Different job keys for the same account serialize through the existing account
lock. Accepted human matches and rejected candidate edges are preserved. The
reconciliation service's unchanged-state checks, revisions, unique constraints and
atomic audit writes make repeated execution safe without claiming exactly-once
invocation. A duplicate message unable to acquire a locked row can be skipped;
the queued row remains available to a later dispatch sweep.

PostgreSQL is authoritative; RQ's finished/failed registries are not public job
status. Migration `0006_job_recovery` makes old queued jobs dispatchable, reopens
old zero-attempt dispatch failures, and makes old running leases immediately
recoverable. It preserves old successes and permanent failures. Downgrade with
stored jobs is refused to avoid discarding recovery state.

## Verification

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
export TEST_REDIS_URL="$REDIS_URL"
make test-jobs
make test
```

Tests use migrated disposable PostgreSQL schemas, unique Redis queues and real
worker subprocesses. SQL triggers/sequences inject deterministic failures that
survive transaction rollback; no public failure controls are added. Coverage
includes transient recovery, permanent failure, exhaustion/backoff, commit/enqueue
gaps, ambiguous sends, Redis message loss, killed workers, locked-transaction
protection, duplicate delivery, stale attempts, completion-write rollback, migration
of existing jobs, authentication and human decisions. Missing test backends skip;
configured connection failures fail. Redis databases are never flushed.
