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
| `GET /accounts` | Account page |
| `POST /receipts` | `merchant`, `receipt_date` (YYYY-MM-DD), `amount` (decimal **string**), uppercase three-letter `currency`, `source`, `source_receipt_id` → `receipt_id`, `created` (200 for first submission and retries) |
| `GET /receipts` | Receipt page; optional `account_id` restricts membership; without it includes unassigned receipts |
| `POST /accounts/{account_id}/receipts/assign` | `{ "receipt_ids": ["UUID"] }` (1–100) → `assigned` count; repeated assignment to the same account returns zero |
| `POST /accounts/{account_id}/transactions/import` | `{ "csv_text": "source_transaction_id,date,merchant,amount,currency\n..." }` (up to 2,000,000 characters) → `inserted`, `skipped`; whole-file validation and atomic import |
| `GET /accounts/{account_id}/transactions` | Stored transaction page |
| `POST /accounts/{account_id}/reconciliation` | No body; synchronously reconciles the full account → `matched`, `needs_review`, `unmatched`, `unmatched_transactions`, `decisions_written` |
| `POST /accounts/{account_id}/reconciliation-jobs` | `{ "request_key": "stable-key" }` → 202 with job ID/status and Location; [worker setup, idempotency and limitations](background-reconciliation.md) |
| `GET /accounts/{account_id}/reconciliation-jobs/{job_id}` | Persisted job status, attempt count, timestamps, result summary and safe failure details |
| `GET /accounts/{account_id}/outcomes` | Current persisted decision page with revisions and evidence; no run history or refresh |
| `GET /accounts/{account_id}/review-cases` | Persisted needs-review snapshots with revisions, active candidates, and rejection history |
| `POST /accounts/{account_id}/review-cases/{receipt_id}/confirm` | Review body below; optionally `acknowledge_amount_mismatch` (strict boolean, default false) |
| `POST /accounts/{account_id}/review-cases/{receipt_id}/reject` | Review body below; rejects this pair only |
| `GET /accounts/{account_id}/audit-events` | Audit page including ingestion, assignment, reconciliation and human-review events |

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
by receipt UUID. Pagination is not a frozen snapshot across requests. Review case
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
