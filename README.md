# Ledger Reconciliation

A backend service for importing bank transactions and structured receipts,
safely reconciling them, reviewing uncertain candidates, and preserving an audit
history. The focus is correctness under duplicate submissions, concurrent
decisions, and database failures.

**Current interface:** an authenticated, synchronous FastAPI interface for a
single-user demo, alongside Python services and local demo scripts. See the
[HTTP setup, endpoint contracts, and repeatable demo](docs/api.md).

## Implemented

| Component | Behavior | Details |
| --- | --- | --- |
| FastAPI demo | Bearer-token authentication, validated JSON contracts, bounded collection pages, Swagger, and account-scoped history | [HTTP API](docs/api.md) |
| PostgreSQL persistence | SQLAlchemy models and Alembic migrations for accounts, receipts, transactions, reconciliation decisions, review actions, and audit events | [Models](src/ledger/persistence/models.py), [migrations](migrations/versions/) |
| Structured receipt ingestion | Validated dates, Decimal amounts, currency, and source identity; duplicate submissions return the same receipt ID | [Receipt ingestion](docs/receipt-ingestion.md) |
| Bank CSV ingestion | Account-scoped records with signed amounts; whole-file validation, atomic imports, and inserted/skipped summaries | [CSV format and ingestion](docs/bank-ingestion.md) |
| Safe reconciliation | Explicit account scope, exact amounts, ambiguity checks in both directions, and persisted match/review/unmatched outcomes | [Matching rules and decisions](docs/reconciliation.md) |
| Human review | List cases, confirm candidates, or reject specific pairs; revision checks and idempotent requests protect decisions | [Review services](docs/human-review.md) |
| Audit history | Ingestion, scope assignment, reconciliation revisions, and reviewer actions recorded with their evidence | [Transaction and audit guarantees](docs/human-review.md#revisions-idempotency-and-transactions) |

The persistence layer uses Python 3.12, SQLAlchemy 2, PostgreSQL, and Alembic.
Services own database transactions that commit state changes and audit events
together. Unique constraints prevent duplicate identities and multiple accepted
matches for either record; composite foreign keys enforce account scope.
Account row locks serialize reconciliation and review, while expected revisions
reject stale human decisions.

Duplicate protection uses stable caller-supplied identities:

- Receipts: `(source, source_receipt_id)`.
- Bank transactions: `(account_id, source_transaction_id)`.
- Review requests: `(account_id, request_id)`.

Identical retries reuse the existing result without duplicate creation/action
events. Reusing an identity with conflicting input is rejected. Distinct source
IDs preserve distinct receipts or payments even when merchant, date, and amount
are identical.

## Run locally

Prerequisites: Make, Docker with Compose, and Python 3.12. `uv` is recommended;
the Makefile uses it when available and otherwise uses `python3.12` and pip.
Run commands from the repository root.

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
make demo-review
```

Compose runs PostgreSQL 16 on localhost port **55432**, with local development
credentials and a named volume. `docker compose stop` stops it while retaining
data. The commands above run service demos. To start the authenticated HTTP API,
configure the demo token and identity and run `make api` as described in the
[API setup](docs/api.md#setup-and-http-walkthrough).

| Environment variable | Used by |
| --- | --- |
| `DATABASE_URL` | Alembic migrations and database demo scripts; use a `postgresql+psycopg://` connection URL |
| `LEDGER_DEMO_TOKEN` | Required bearer secret for all API business routes; generate locally, never commit |
| `LEDGER_DEMO_IDENTITY` | Required single demo reviewer identity derived by the API from authentication |
| `TEST_DATABASE_URL` | PostgreSQL integration tests; the user must have permission to create and drop test schemas |

Service functions take a SQLAlchemy engine explicitly. See the feature docs above
for Python usage, account provisioning, and input contracts.

### Available demos

After setup and migrations, each demo can be run independently:

| Command | Demonstrates |
| --- | --- |
| `make demo-ingestion` | Submit one receipt twice: same UUID, one receipt, one creation event |
| `make demo-bank-ingestion` | Import the [sample CSV](data/samples/bank-transactions.csv) twice: unchanged transaction and creation-event counts |
| `make demo-reconciliation` | A clear match, an amount mismatch, an ambiguous case, and an unmatched receipt |
| `make demo-api` | HTTP ingest → reconcile → review → audit history against a running API; see [setup](docs/api.md) |
| `make demo-review` | Confirm an ambiguous candidate and reject another pair; reconciliation preserves both actions without duplicate human-action events |

Demos persist their data and reuse stable identities on subsequent runs.

### Tests

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres   # persistence, reconciliation, review, and PostgreSQL API tests
make test            # complete suite, including extraction/evaluation tests
```

Integration tests apply migrations in isolated schemas and remove those schemas
afterward. They cover persistence, duplicate imports, concurrent submissions,
one-to-one matching, stale review revisions, rejected candidates, and rollback
when audit writes fail. Existing application tables are not cleared.

Without `TEST_DATABASE_URL`, database tests explicitly skip; `make test-postgres`
requires it. Configured database connection failures fail the tests.

## Current matching rules and limits

Receipts start unassigned. The caller explicitly assigns them to one account
using `assign_receipts_to_account`; `reconcile_account` considers all receipts
assigned to that account and its stored bank transactions. Unassigned receipts
and records from other accounts cannot participate.

Automatic acceptance requires:

1. A positive receipt total and a negative bank debit, with identical currency.
2. A transaction date 0–4 calendar days after the receipt date, inclusive.
3. Guarded merchant equality: recognized processor prefixes and punctuation are
   normalized, while meaningful tokens such as store numbers are preserved.
4. Exact Decimal equality, `receipt.amount == -bank.amount`, and a unique
   plausible pair in both directions.

Amount differences remain review candidates rather than disappearing behind an
amount filter. Ambiguous pairs are not resolved by a score or greedy assignment;
records without plausible candidates remain unmatched. Human confirmation of an
amount mismatch requires explicit acknowledgement and a reason, preserving both
original amounts. Rejections exclude only the selected receipt/transaction pair
and keep its remaining candidates under human review on reruns.

Accepted matches stay fixed; undo, reopening, and account reassignment are
deferred. Refunds, split payments, currency conversion, fuzzy merchant aliases,
and longer settlement windows are not supported. Amounts use `NUMERIC(18,2)`;
currency validation checks code shape rather than currency-specific minor units.
Python services accept caller-supplied reviewer identity; the HTTP API derives it
from the one authenticated demo identity and rejects client reviewer fields. This
is a single-user demo with access to all accounts, without registration or
multi-user authorization. Services append audit events, but database
administrator tamper protection is not implemented. See the
[reconciliation](docs/reconciliation.md) and [review](docs/human-review.md) docs
for full rules and migration limitations.

## Supporting extraction and evaluation work

The original pipeline remains separate from the PostgreSQL services. It includes
rules-based header extraction from corpus-provided OCR text, a heuristic baseline
matcher, frozen vendor-grouped dataset splits, a versioned corruption model, and
an evaluation harness. It does not provide receipt-image ingestion for the backend.

```sh
make eval          # synthetic dev evaluation; no dataset download needed
make data          # optional: download SROIE OCR text and annotations
make eval-sroie    # evaluate the SROIE dev split
```

Evaluation commands write JSON results to `evals/`. Receipt-to-transaction
evaluation uses generated bank records under the
[corruption model](src/ledger/data/corruption.py); its scores are not evidence of
backend matching accuracy on real bank statements. Historical outputs live in
[evals/](evals/), with implementation in [data](src/ledger/data/),
[baseline](src/ledger/baseline/), and [eval](src/ledger/eval/). Model training and
receipt-image extraction are not prerequisites for the backend MVP.

## Planned

- **Background processing:** durable jobs with bounded retries and failed-job
  handling.
- **Dashboard:** browse imports, reconciliation outcomes, review cases, and history.
- **Deployment:** package and host a demonstrable backend and dashboard.

These remain future milestones. The current implementation is exercised through
the HTTP API, Python services, tests, and local demos above.
