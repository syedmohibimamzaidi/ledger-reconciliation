# Single-user demo API

The FastAPI interface reuses the existing PostgreSQL services, with optional
[background reconciliation jobs](background-reconciliation.md). No registration,
user store, account ownership, multi-user authorization, UI,
or deployment is provided. Anyone holding the one configured token can read and
mutate **all** accounts and receipts. Account scope enforces reconciliation
membership; it is not an authorization boundary.

## Setup and HTTP walkthrough

From the repository root:

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
# Generate your own secret locally; no application token is checked into Git.
export LEDGER_DEMO_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LEDGER_DEMO_IDENTITY='local-demo-reviewer'
make api
```

Keep this server terminal open. In another terminal, export the **same** token
as `LEDGER_DEMO_TOKEN` (do not generate another), then run:

```sh
make demo-api
curl --fail -H "Authorization: Bearer $LEDGER_DEMO_TOKEN" \
  'http://127.0.0.1:8000/accounts?limit=10&offset=0'
```

The HTTP script creates/reuses its named demo account, submits and assigns a
structured receipt, imports [sample transactions](../data/samples/bank-transactions.csv)
twice, reconciles, confirms an ambiguous candidate, repeats the confirmation,
and prints outcomes and audit history. It uses only HTTP and can be rerun
sequentially against its own unchanged demo data; reruns preserve the accepted
match and its single human-action event. It is not a concurrent account-provisioning
tool. `LEDGER_API_URL` optionally changes its target (default localhost:8000).

Swagger UI is at <http://127.0.0.1:8000/docs>; OpenAPI JSON is at
<http://127.0.0.1:8000/openapi.json>. Use **Authorize** and paste the token without
`Bearer `. Documentation endpoints are public and contain no stored business
data. Every business operation declares HTTP Bearer security in OpenAPI.

## Contracts

All paths are relative to `http://127.0.0.1:8000`. Business requests require
`Authorization: Bearer <token>`. All request and response bodies are JSON.

| Method and path | Body / result |
| --- | --- |
| `POST /accounts` | `{ "name": "Checking" }` → account with UUID and timestamp (201); duplicate names are allowed |
| `GET /accounts` | Account page, including nullable `last_reconciled_at` |
| `GET /accounts/{account_id}/summary` | One-snapshot account counts, last successful reconciliation cutoff, new inputs, active jobs and latest job |
| `POST /receipts` | `merchant`, `receipt_date` (YYYY-MM-DD), `amount` (decimal **string**), uppercase three-letter `currency`, `source`, `source_receipt_id` → `receipt_id`, `created` (200 for first submission and retries) |
| `GET /receipts` | Receipt page; optional `account_id` restricts membership; `unassigned=true` selects only unassigned receipts (cannot combine with `account_id`) |
| `GET /receipts/{receipt_id}` | Receipt details, including nullable current `decision`; works for unassigned receipts |
| `POST /accounts/{account_id}/receipts/assign` | `{ "receipt_ids": ["UUID"] }` (1–100) → `assigned` count; repeated assignment to the same account returns zero |
| `POST /accounts/{account_id}/transactions/import` | `{ "csv_text": "source_transaction_id,date,merchant,amount,currency\n..." }` (up to 2,000,000 characters) → `inserted`, `skipped`; whole-file validation and atomic import |
| `GET /accounts/{account_id}/transactions` | Stored transaction page |
| `GET /accounts/{account_id}/transactions/{transaction_id}` | Account-scoped transaction details; wrong account returns 404 |
| `POST /accounts/{account_id}/reconciliation` | No body; synchronously reconciles the full account → `matched`, `needs_review`, `unmatched`, `unmatched_transactions`, `decisions_written` |
| `POST /accounts/{account_id}/reconciliation-jobs` | `{ "request_key": "stable-key" }` → 202 with job ID/status and Location; [worker setup, idempotency and recovery](background-reconciliation.md) |
| `GET /accounts/{account_id}/reconciliation-jobs` | Newest-first job page; optional `status` filters the display states `queued`, `retrying`, `running`, `succeeded`, `failed` |
| `GET /accounts/{account_id}/reconciliation-jobs/{job_id}` | Persisted job status, attempt count/budget, retry/dispatch/lease timing, timestamps, result summary and safe failure details |
| `GET /accounts/{account_id}/outcomes` | Current persisted decision page; optional `status` (`matched`, `needs_review`, `unmatched`) and `receipt_id` filters; no refresh |
| `GET /accounts/{account_id}/review-cases` | Persisted needs-review snapshots with revisions, active candidates, and rejection history |
| `POST /accounts/{account_id}/review-cases/{receipt_id}/confirm` | Review body below; optionally `acknowledge_amount_mismatch` (strict boolean, default false) |
| `POST /accounts/{account_id}/review-cases/{receipt_id}/reject` | Review body below; rejects this pair only |
| `GET /accounts/{account_id}/audit-events` | Audit page; `order=oldest` (default) or `order=newest` |

Review body:

```json
{
  "bank_transaction_id": "7f83ff16-cdbf-4c11-b367-aa1d4a62d9c1",
  "request_id": "d0d4efba-f74f-4587-9a97-5749011521ca",
  "expected_revision": 1,
  "reason": "Verified statement reference"
}
```

Review responses contain `action_id`, `receipt_id`, resulting `revision`, `status`,
`reviewed_at`, and `repeated`. Retain the request UUID and entire normalized body
for retries. Exact retries return the original result even after later actions;
new actions require a fresh UUID and current revision. A changed body under an
existing UUID conflicts. The server always supplies `LEDGER_DEMO_IDENTITY` as
reviewer; a client-supplied `reviewer` field is rejected. Confirming differing
amounts requires `acknowledge_amount_mismatch: true` and a reason.

Receipt input rejects JSON numbers, nonfinite amounts, negative amounts, fractional
cents and amounts outside NUMERIC(18,2). All money in responses, including signed
bank amounts and nested service evidence/audit snapshots, uses exact two-place
strings. Requests reject unknown fields. Text is trimmed, bounded, nonblank and
NUL-free; IDs must be UUIDs, revisions positive strict integers. CSV validation
and normalization use the existing [CSV service contract](bank-ingestion.md).

Every collection returns `{ "items": [...], "limit": 50, "offset": 0 }`.
`limit` defaults to 50 and is bounded to 1–100; `offset` defaults to 0 and is bounded
to 0–100000. Stored rows order by creation timestamp then UUID; review cases order
by receipt UUID. Job lists order by creation timestamp and UUID descending; audit
order is selectable and defaults to the original oldest-first behavior. Offset
pagination is not a frozen snapshot: newly inserted jobs/events can shift later
pages, causing duplicates or skipped rows. This is accepted for the single-user
demo; keyset paging on `(created_at, id)` is deferred.

Bank import, assignment, reconciliation and review events are timestamped with
wall-clock time read after the account lock, never the earlier transaction start.
A run or review that waited behind an import therefore sorts after the inputs it
used. All decision events from one run share that run's cutoff timestamp, and the
UUID tiebreaker does not encode execution order. Group equal timestamps in the
dashboard; do not imply ordering within such a group. Receipt creation events are
not account-scoped writes and still use their own transaction time.
An audit sequence column is deferred. Review case
pagination bounds HTTP responses; the existing service still loads the account's
full coherent case list. Reconciliation processes the complete account regardless
of pagination. Audit scope follows the current membership of immutable assigned
receipts and bank transactions, including receipt creation before assignment.

Errors use `{ "error": { "code": "...", "message": "...", "details": null } }`:

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `unauthorized` | Missing/invalid bearer token; includes `WWW-Authenticate: Bearer` |
| 404 | `not_found` | Missing account, receipt, transaction, or reconciliation case within selected scope |
| 422 | `invalid_input` | Schema/service validation or ineligible candidate; details contain field locations or CSV row errors |
| 409 | `identity_conflict` | Conflicting receipt, transaction, or review request identity |
| 409 | `stale_revision` | Details contain `expected_revision` and `actual_revision`; reload the case |
| 503 | `jobs_unavailable` | New background submission without Redis configuration; synchronous routes remain available |
| 409 | `state_conflict` | Already assigned elsewhere, rejected/accepted candidate, or another incompatible current state |

## Dashboard summary semantics

`GET /accounts/{account_id}/summary` performs **one PostgreSQL SELECT** with FILTER
aggregates and scalar subqueries. Its counts, latest job and cutoff share one MVCC
snapshot under READ COMMITTED. It takes no account row lock and does not call
`list_review_cases`. Poll the summary (and known active job statuses) for overview
updates. `/review-cases` still locks the account for a coherent review snapshot:
load it on navigation or explicit refresh, rather than polling it.

The response has these groups:

- `receipts`: `total`, `matched`, `needs_review`, `unmatched`, `without_decision`,
  `assigned_since_last_run`. The three outcome counts plus `without_decision` add
  up to `total`. They describe persisted decisions, which may need refreshing.
  **Do not label `without_decision` as all "not yet reconciled" receipts.**
- `transactions`: `total`, `matched_debits`, `unmatched_debits`,
  `credits_not_eligible`, `zero_amount_not_eligible`, `imported_since_last_run`.
  The four amount/match categories partition `total`. Only negative amounts count
  as debits; credits and zero amounts never enter the receipt matching queue.
- `active_jobs`: mutually exclusive `queued`, `retrying`, `running` counts.
- `latest_job`: the newest submitted job or null. It includes existing public job
  fields plus `display_status`. Historical failures do not create a permanent
  "failed jobs" headline. A newer success replaces an older failure here.

`display_status=retrying` means stored `status=queued`, `attempt_count > 0`, and a
future `next_attempt_at`, evaluated using the database statement time. Once due it
is displayed as queued. Job list `status` filters use that same display state;
`status=queued` excludes delayed retries. The existing single-job endpoint keeps
its original stored status contract; clients can derive the same display label.

`last_reconciled_at` is an account field written inside every **successful** sync
or background reconciliation transaction, including unchanged and empty runs.
It is the input cutoff captured after the account lock is acquired, rather than
a decision row's last modification time or the job completion time. Failures roll
back the cutoff along with decisions/audits. Migration `0007_dashboard_reads`
leaves existing accounts null until a real run; no historical run time is inferred.

`assigned_since_last_run` compares immutable `receipt.scoped` event times to that
cutoff; receipt creation time is not assignment time. `imported_since_last_run`
compares stored bank creation times. With a null cutoff all account inputs count
as new. Duplicate imports and repeated assignments do not increment either count.
`has_new_reconciliation_inputs` is true when either count is nonzero. This flags
new data even if all receipts already have old decision rows. It is not a claim
that every persisted decision is fresh: human actions may also affect other cases,
and direct database edits are outside the service contract.

To keep concurrent imports visible, bank imports take a shared KEY SHARE lock on
the account before inserting; imports can still run in parallel but serialize with
reconciliation's UPDATE lock. Their creation times use `clock_timestamp()` after
that lock. Scope events likewise record wall-clock time after the assignment
lock. A transaction that began earlier but waited behind a reconciliation run is
therefore counted as new. The cutoff and all counts refer to the current account;
unassigned and other-account records are excluded.

Receipt `status` remains a compatibility field fixed to `pending`. The dashboard
must use the receipt detail's `decision.status` (or filtered outcomes) for match
state, with no decision rendered explicitly. Detail and summary endpoints are
read-only and do not perform reconciliation. Missing detail records return 404;
valid collection filters with no matches return an empty page. All new reads
require the same bearer authentication and bounded pagination as existing routes.

The synchronous/job result field `unmatched_transactions` retains its historical
all-transactions-minus-matches definition for compatibility; it includes credits
and zero amounts. Dashboard headline counts must use `unmatched_debits` instead.
The job listing has an `(account_id, created_at, id)` index. Persistent import
batches/history, audit sequences and keyset pagination remain deferred.

## Authentication and transaction boundaries

Application construction fails closed if either authentication variable is absent,
blank or invalid. Token comparison is constant-time. The token must be printable
ASCII without whitespace; identity must fit the service's nonblank 255-character
reviewer field. Configuration is read once per application instance; rotate by
restarting with a new token. Changing identity also changes review request identity
for idempotency comparisons. There is no expiry, token issuing endpoint, per-user
access control or production identity assurance. Bind locally for this demo.

The engine pool belongs to application lifespan and is disposed on shutdown.
Each read connection closes after use. Existing services receive the engine and
own their READ COMMITTED transactions; no request-scoped ORM transaction wraps
or commits their work. Creation/assignment are explicitly separate operations:
a successful submission remains unassigned if a later assignment fails, and may
be safely retried. Imports, reconciliation and review retain their existing atomic
state-plus-audit guarantees, locks, revision checks and duplicate protection.
Background reconciliation reuses the same rules inside a fenced transaction that
commits job success, decisions and audit events together; its durable dispatcher
and bounded retries are described in the [recovery contract](background-reconciliation.md).
Account creation uses its own short transaction. No migrations run on server start.

## Verification

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres
make test
```

API tests use real PostgreSQL in disposable migrated schemas and cover Swagger,
OpenAPI security, all-route authentication, fail-closed setup, exact amounts,
validation, duplicate imports, reconciliation, confirm/reject retries, stale
revisions, account isolation of history, audit rollback, and an end-to-end HTTP
walkthrough. No configured database means explicit skips; connection failures fail.
