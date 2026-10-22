# Human review of reconciliation candidates

Caller-driven Python services list review cases, confirm a candidate, and reject
one receipt/transaction pair. Reviewer identity is supplied by the caller;
authentication, API endpoints, undo, and reopening are deferred. Existing ingestion
identities, account scope, and the independent evaluation pipeline remain intact.

## Local demonstration

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
make demo-review
```

The demo creates a separate account and two ambiguous cases, each with two bank
candidates. It confirms one candidate for the first case, rejects a specific
candidate for the second, and reruns reconciliation. It asserts:

```text
Confirmed case after rerun: matched, human_action_audit_events=1
Rejected candidate remains excluded; other candidate remains available for review
Reconciliation decision writes after human actions: 0
```

Stable ingestion identities and request UUIDs allow repeated script runs without
new actions or audit events. On subsequent runs only the rejection case remains
in the review list and the confirmation is reported as an idempotent retry.

## Service contract

Populate/refresh cases using `reconcile_account`, then list persisted snapshots:

```python
from uuid import UUID, uuid4
from ledger.persistence.review import (
    list_review_cases, confirm_candidate, reject_candidate,
)

case = list_review_cases(engine, account_id)[0]
candidate_id = UUID(case.candidates[0]["bank_transaction_id"])
request_id = uuid4()  # generate once; retain unchanged for retries
result = confirm_candidate(
    engine, account_id, case.receipt_id, candidate_id,
    expected_revision=case.revision, request_id=request_id,
    reviewer="reviewer@example.test", reason="Verified statement reference",
    acknowledge_amount_mismatch=False,
)
```

`ReviewCase` includes account and receipt UUIDs, revision, receipt fields, active
candidate snapshots, reason codes, and rejection history with reviewer, reason,
and timestamp. Only `needs_review` cases are listed. Listing is read-only; it does
not refresh automatic outcomes. A candidate may have become unavailable through
another case since its snapshot was calculated; confirmation always rechecks.

For rejection use the same argument shape without the acknowledgement flag:

```python
result = reject_candidate(
    engine, account_id, case.receipt_id, candidate_id,
    expected_revision=case.revision, request_id=uuid4(),
    reviewer="reviewer@example.test", reason="Duplicate authorization",
)
```

All IDs are UUID objects. Revisions must be positive integers. Every action
requires a nonblank reviewer (at most 255 characters) and reason (at most 2000).
Outer whitespace is trimmed and NUL characters are rejected. These identity
strings are audit attribution, not proof of authentication or authorization.

## Confirmation and original amounts

Confirmation resolves a `needs_review` case to `matched`, incrementing its revision
once. The selected transaction must be an active candidate in that case snapshot
and still satisfy the existing account, guarded merchant, date (0–4 days), currency,
positive receipt total, and negative debit rules. A rejected candidate cannot be
confirmed. A transaction already accepted for another receipt cannot be claimed.
Human selection resolves ambiguity without bypassing eligibility rules.

If `receipt.amount != -bank.amount`, confirmation additionally requires the literal
boolean `acknowledge_amount_mismatch=True` and a reason. Both original signed
Decimal amounts remain unchanged in their ingestion records. They are also stored
as NUMERIC values in the human action, and as exact strings in action/audit
snapshots. Acknowledgement permits acceptance without correcting either amount.

Accepted matches remain fixed, whether automatic or human. A fresh request cannot
replace or reject one; only an identical retry of the original action succeeds.

## Per-candidate rejection and reruns

Rejection records a durable edge `(receipt_id, bank_transaction_id)` and increments
the case revision once. It does not blacklist the bank transaction for other
receipts or reject other candidates. The selected case is recomputed with that
edge removed. Other cases are refreshed on the next reconciliation run.

If other candidates remain, the case stays `needs_review` with
`human_review_required`. Reconciliation cannot implicitly confirm a remaining
candidate after a reviewer rejects an alternative. If none remain, the case
becomes `unmatched` with `all_candidates_rejected`; rejection and audit history
remain stored. Newly arriving candidates return that case to human review.

Reconciliation removes rejected edges before testing ambiguity in either direction.
Unreviewed cases retain their original automatic behavior, including acceptance
when they become clear and unique. Accepted matches remain fixed and stay in the
competitor graph, as before.

## Revisions, idempotency, and transactions

Each operation owns one PostgreSQL READ COMMITTED transaction and locks the same
account row as scope assignment/reconciliation. Concurrent actions serialize before
checking state. A new action must supply the current `expected_revision`;
otherwise `StaleReview` exposes the expected and actual revisions. A candidate
claimed through another case raises `ReviewConflict`. Invalid input, scope, or
eligibility raises `ValueError`. Failures write nothing.

`request_id` is unique within an account. An exact normalized retry returns the
stored `ReviewResult` with `repeated=True`, including its original resulting
revision, status, and timestamp. This check precedes revision validation, so a
lost response can be retried after later actions. It returns the historical result
without changing the current case. Reusing the request ID with a different receipt,
candidate, action, expected revision, reviewer, reason, or acknowledgement raises
`ReviewConflict`. New intended actions need new request UUIDs and the latest revision.

The case update, `review_actions` record, and **one** `receipt.reviewed` audit event
commit atomically. The event includes reviewer, action, reason, timestamp, old/new
revisions, original receipt/transaction snapshots, mismatch acknowledgement, and
resulting decision details. A human action does not also write an automatic
`receipt.reconciliation_decided` event. Audit failure rolls back the action, case
revision, accepted link, and rejected edge; the request can then be retried.

Database constraints enforce account-scoped references, existing one-to-one
accepted matches, unique request IDs, one human action per resulting case revision,
one confirmation per receipt, one rejection per edge, and one audit event per
human action. A check rejects confirmation of differing stored action amounts
without acknowledgement. Service transactions enforce full state transitions and
the entity/event contract; administrator-level audit immutability is not added.

Migration `0004_human_review` is reversible while empty. It explicitly refuses
downgrade once human actions exist, preserving accepted decisions and rejection
history while undo/reopening remains deferred. Earlier migration compatibility
checks still run against isolated empty-action schemas.

## Tests

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres
make test
```

PostgreSQL tests cover listing, confirmation, mismatch acknowledgement and original
amounts, pair-specific rejections, reruns/new candidates, account and live eligibility
checks, stale revisions, retries and changed requests, concurrent confirmations,
one-to-one protection, audit rollback, database constraints, and refusal of populated
downgrade. Existing ingestion, reconciliation, and evaluation suites are retained.
Database tests skip without TEST_DATABASE_URL; configured connection failures fail.
`make test-postgres` requires the URL.
