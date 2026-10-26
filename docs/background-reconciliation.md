# Background reconciliation: worker execution and persistent status

This milestone adds a separate RQ worker and PostgreSQL job records. The existing
synchronous reconciliation endpoint, imports, review services and matching rules
remain compatible. This is still the single-user demo: its bearer token grants
access to all accounts. Job account scope enforces data membership, not user
ownership. See [authentication](api.md#authentication-and-transaction-boundaries).

**Delivery is best effort.** Automatic retries, reliable dispatch recovery and
interrupted-worker recovery are deferred. Persistent status is not a guarantee
that a queued job will run or that a running job will receive a terminal status.

## Local setup and repeatable HTTP demo

Run from the repository root:

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

In a second terminal, export the same `DATABASE_URL` and `REDIS_URL`, then:

```sh
make worker
```

The worker is a separate Python process; the API never executes submitted jobs
in an in-process background task. The demo uses RQ's `SimpleWorker` to avoid
forking on macOS. It executes one task at a time, without periodic in-task
heartbeats. The queue uses JSON serialization on both sides and a 300-second
task timeout. No retry policy or scheduler is enabled. This is a local demo
worker configuration, not a production supervisor.

In a third terminal, export the same API token as `LEDGER_DEMO_TOKEN`, then:

```sh
make demo-jobs
```

The script uses HTTP only: it creates/reuses its named demo account, submits and
assigns a structured receipt, imports the sample CSV, submits a reconciliation
job, prints the HTTP 202 job ID, replays the same request to verify its identity,
and polls until success. Typical output:

```text
HTTP 202: job_id=<UUID>
Status: queued
Status: succeeded
Result: {'matched': 0, 'needs_review': 1, 'unmatched': 0, 'unmatched_transactions': 3, 'decisions_written': 1}
```

Fast jobs may finish before the first poll, so intermediate states may not appear.
The sample contains two plausible payments: successful job execution correctly
produces a review case rather than an automatic match. Sequential reruns reuse
ingested records and submit a fresh request key for each intended run; unchanged
outcomes then produce zero decision writes. The script stops after 30 seconds if
no worker completes it. `LEDGER_API_URL` defaults to `http://127.0.0.1:8000`.

| Configuration | Purpose |
| --- | --- |
| `DATABASE_URL` | Required by API/worker; both must target the same migrated PostgreSQL database/schema |
| `REDIS_URL` | Redis connection URL used by API and worker; Compose exposes Redis 7 at localhost **56379** |
| `LEDGER_QUEUE_NAME` | Optional shared queue name; defaults to `ledger-reconciliation` |
| `LEDGER_DEMO_TOKEN`, `LEDGER_DEMO_IDENTITY` | Existing API authentication; the worker does not need the bearer secret |
| `TEST_DATABASE_URL`, `TEST_REDIS_URL` | Explicit integration-test backends |

Keep connection URLs and bearer secrets in the environment. The Redis service
binds locally and has no persistent volume; queue contents may be lost when its
container is recreated. PostgreSQL job status survives independently.

## HTTP contracts

Both endpoints require the existing bearer authentication and appear in Swagger
at `/docs` and OpenAPI at `/openapi.json`.

| Method and path | Contract |
| --- | --- |
| `POST /accounts/{account_id}/reconciliation-jobs` | JSON `{ "request_key": "caller-stable-key" }`; returns **202** with the persisted job and a `Location` status URL |
| `GET /accounts/{account_id}/reconciliation-jobs/{job_id}` | Returns **200** with the current PostgreSQL job record, or **404** for a missing account/job or a job from another account |

The record includes `id`, `account_id`, `request_key`, `status`, `attempt_count`,
`created_at`, `updated_at`, nullable `started_at`/`finished_at`, nullable
`result_summary`, and nullable `failure_details`. Success summary fields match
the synchronous response: `matched`, `needs_review`, `unmatched`,
`unmatched_transactions`, `decisions_written`. It describes that worker run;
current outcomes remain available through the existing outcomes endpoint.

Request keys are nonblank, trimmed, NUL-free strings of at most 255 characters.
They are globally unique for reconciliation submissions in this demo. The stored
normalized payload consists of operation/version and the path's account UUID;
there are no job options or caller-supplied account/identity overrides. Identical
key and payload replay returns the same job ID and its current status with 202,
including terminal jobs. Reusing a key with a different account returns **409**
`identity_conflict`. Unknown body fields and invalid IDs/keys return **422**.
Replaying a key neither dispatches again nor reruns reconciliation over newer
data. Use a new key for a new intended reconciliation run.

If `REDIS_URL` is absent, new submissions return **503** `jobs_unavailable`
without retaining a new row. Existing-key replay, status reads, and synchronous
endpoints remain available without Redis configuration. If dispatch raises an
error after a job is committed, submission still returns **202** with the job ID;
its status normally becomes `failed` with `dispatch_failed`. Always inspect/poll
the status. A concurrently claimed job retains its actual running/terminal state.

## Execution and transaction boundaries

1. The API commits a `queued` job and its unique request identity in PostgreSQL.
   A concurrent identical submission reuses the row. Only the insert winner
   attempts to enqueue the UUID in Redis through RQ.
2. The separate worker receives only that job UUID. An atomic PostgreSQL update
   changes `queued` to `running`, increments the attempt count to one and records
   the start time. Unknown IDs and already running/terminal jobs are skipped.
   The account UUID is read from PostgreSQL, never from the queue payload.
3. The worker calls the existing `reconcile_account` service with its own engine.
   The service retains its complete READ COMMITTED transaction, account lock,
   human acceptance/rejection logic, decision revisions, and atomic audit writes.
4. The worker commits `succeeded` and the summary, or `failed` and fixed safe
   failure details. Service exceptions are not stored or returned as raw SQL,
   connection strings, stack traces, or merchant/receipt content.

States are `queued` → `running` → `succeeded`/`failed`; a dispatch failure can move
`queued` directly to `failed`. Attempt count is zero until a worker claims the
job, then one. There are no automatic or manual job retries yet. Both terminal
states are protected from reprocessing, even if duplicate queue messages arrive.
Jobs with different keys for the same account still serialize through the existing
account lock. Imports and human review retain their existing behavior.

PostgreSQL is authoritative for public status; RQ is only transport/execution.
A handled service failure is recorded in PostgreSQL and returns normally from the
RQ task, so RQ's own finished registry is not a business success indicator. Redis
result metadata can expire immediately; PostgreSQL job records remain. Migration
`0005_reconciliation_jobs` refuses downgrade once jobs exist, to avoid silently
discarding pending work or durable execution records.

## Temporary failure limitations

- PostgreSQL insertion and Redis enqueue are separate commits. A process crash
  between them can leave a permanently `queued` job. There is no transactional
  outbox, dispatch sweep, or replay-driven redispatch.
- Redis disconnects can make enqueue results ambiguous. An unclaimed job is
  marked `failed` when possible; a delivered copy then skips that terminal row.
  A worker that already claimed it can still finish. Redis message loss after a
  successful enqueue can leave `queued` indefinitely.
- A worker crash, kill, timeout, or database outage can prevent status updates.
  Such a job may remain `running` (or `queued` if claiming failed). There are no
  leases, application heartbeats, abandoned-job sweeps, or worker recovery.
- The reconciliation transaction commits separately from the final status write.
  A crash after reconciliation commits can leave `running` even though decisions
  and audit events were committed. There is no atomic run-completion guarantee.
- Replaying the original request returns its existing status without recovery.
  A new key requests a separate run against then-current account data. This is
  not reliable delivery, exactly-once execution, or an operator recovery protocol.

## Checks

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
export TEST_REDIS_URL="$REDIS_URL"
make test-jobs       # requires both configured services
make test-postgres  # existing PostgreSQL API/service coverage
make test           # full suite, including jobs when both URLs are configured
```

Job integration tests use migrated disposable PostgreSQL schemas, uniquely named
Redis queues, and separate worker subprocesses. They cover authentication,
submission/status, concurrent and conflicting identities, visible running state,
success, duplicate claims/delivery, human-decision preservation, account isolation,
audit rollback, safe service failure, dispatch failure, and operation without
Redis configuration. They do not flush the Redis database. Missing test URLs
explicitly skip the relevant integration tests; configured connection failures
fail. No tests claim delivery or interrupted-worker recovery.
