# PostgreSQL receipt ingestion

This milestone accepts structured receipt fields through a Python service.
It adds no API, OCR, workers, UI, or matching changes. The evaluation dataclasses
and pipeline remain independent of the persistence models.

## Local demonstration

From the repository root, with Docker running:

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
make demo-ingestion
```

The script submits the same receipt twice, prints the same UUID both times,
and checks `receipts=1, creation_audit_events=1` for that source identity.
On its first run, `created=True` then `created=False`; subsequent runs return
`created=False` both times. Counts are scoped to this demo receipt so other data
does not affect the demonstration.

PostgreSQL listens on localhost port 55432. Compose uses local demo credentials
and a named volume so data survives container restarts. `docker compose stop`
stops the database while retaining its data.

## Service contract and duplicate identity

`ledger.persistence.ingest.ingest_receipt(engine, StructuredReceipt(...))`
returns `IngestionResult(receipt_id=<UUID>, created=<bool>)` after commit.

Required fields: merchant, `datetime.date` receipt_date, `Decimal` amount,
currency, source, and source_receipt_id. Floats, negative/nonfinite amounts,
fractional cents, missing/oversized strings, and malformed currency codes are
rejected before database access. Amounts use `NUMERIC(18,2)` without silent
rounding. This milestone supports nonnegative amounts with two decimal places;
currency validation checks three ASCII letters, not an ISO currency registry or
currency-specific minor-unit rules.

**Identity is `(source, source_receipt_id)`**, enforced by the PostgreSQL unique
constraint `uq_receipts_source_identity`. The caller must provide a stable
namespace for the originating system/import and a stable identifier for each
actual receipt. Retrying the same receipt must reuse both. Two distinct receipts
must have distinct identities even when merchant, date, and amount are identical.
For manual entry, generate an ID once and retain it for retries; do not generate
a fresh ID on every submission. File content hashing is outside this milestone.

Outer whitespace is stripped from merchant and identity strings; source and
source_receipt_id remain case-sensitive. Currency is trimmed and uppercased.
Numerically equivalent Decimal amounts compare equally. Reusing an identity with
different normalized merchant/date/amount/currency raises `IdentityConflict` and
does not update the receipt, timestamps, or audit events. Correcting an existing
receipt requires a future explicit update workflow.

## Transactions and concurrency

The service owns a transaction on the supplied PostgreSQL SQLAlchemy engine,
using PostgreSQL's default **READ COMMITTED** isolation. It inserts with
`ON CONFLICT ON CONSTRAINT uq_receipts_source_identity DO NOTHING RETURNING id`.
The database unique constraint arbitrates concurrent inserts. A competing insert
waits for the first transaction: after a commit, a separate SELECT sees the
existing receipt and checks its fields; after a rollback, it can create the row.

Only the transaction that inserts a new receipt writes `receipt.created`.
That event includes a JSONB snapshot with an exact decimal string, source
identity, status, and receipt fields. Receipt and audit creation commit together;
any error rolls back both. A partial unique index additionally prevents a second
creation event for the same receipt. A foreign key prevents dangling events.

Rows start in `pending`; created_at and updated_at are timezone-aware database
timestamps. This milestone has no status transitions or update service, so
updated_at initially equals created_at and retries do not change it.
The service only appends audit events; database-level protection against an
administrator modifying/deleting events is not implemented. Direct SQL writers
must honor the receipt-plus-audit transaction contract.

## Verification

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres
make test
```

Tests create a random schema per test, apply the actual Alembic migration,
and drop that schema afterward. The database user needs CREATE SCHEMA rights.
Existing application tables are not cleared. With TEST_DATABASE_URL set,
connection/setup failures fail the suite; without it database tests explicitly
skip. `make test-postgres` requires the variable so missing configuration cannot
silently count as a successful integration run.

Coverage includes committed Decimal persistence and audit snapshots, identical
retries, distinct identities with identical business fields, changed-content
conflicts, four concurrent submissions on independent connections, direct
constraint enforcement, and a forced PostgreSQL audit-write failure proving
receipt rollback and successful retry. Migration checks compare models against
the migrated schema and exercise downgrade/upgrade in an isolated schema.

The implementation uses SQLAlchemy's PostgreSQL-specific
[ON CONFLICT support](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)
and an [Alembic migration environment](https://alembic.sqlalchemy.org/en/latest/tutorial.html).
