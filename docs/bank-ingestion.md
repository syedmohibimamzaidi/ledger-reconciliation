# Bank transaction CSV ingestion

This service imports bank statement records into an existing PostgreSQL account.
It shares the receipt feature's SQLAlchemy metadata, Alembic environment, and
audit table. It does not run reconciliation or change the evaluation pipeline.

## Local demonstration

From the repository root, with Docker running:

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
make demo-bank-ingestion
```

The demo provisions one account with a stable UUID, then imports
[`data/samples/bank-transactions.csv`](../data/samples/bank-transactions.csv) twice.
On its first run it prints:

```text
First:  inserted=3, skipped=0; transactions=3, creation_audit_events=3
Second: inserted=0, skipped=3; transactions=3, creation_audit_events=3
```

Later script runs skip all three records on both imports. Counts are scoped to
the demo account; other accounts and receipt audit events are unaffected. The
first two sample rows deliberately represent distinct payments with identical
date, merchant, and amount but different source IDs. The third is a credit.

## CSV contract

UTF-8 decoded text, optionally starting with a BOM. Required header names, each
exactly once, in any order:

```csv
source_transaction_id,date,merchant,amount,currency
statement-001,2026-10-22,"Cafe, Main Street",-12.30,CAD
statement-002,2026-10-23,Refund,+4.50,CAD
```

| Column | Rules |
| --- | --- |
| `source_transaction_id` | Stable, case-sensitive ID supplied by the bank/import source; 1–255 characters |
| `date` | Valid calendar date, exactly `YYYY-MM-DD`; stored as `transaction_date` |
| `merchant` | Nonblank merchant/statement description, up to 255 characters |
| `amount` | Exact signed decimal fitting `NUMERIC(18,2)`: absolute value below 10^16 and at most two fractional places |
| `currency` | Three letters, normalized to uppercase; for example `CAD` |

**Negative means debit/outflow; positive means credit/inflow**, including refunds.
The plus sign is optional. Zero is accepted. Sources using the opposite convention
must convert before calling this service. No float conversion or rounding occurs.
Exponent notation, thousands separators, NaN, infinity, and fractional cents are
rejected. Currency validation checks the code shape, not an ISO registry or
currency-specific minor units; amounts use two decimal places for this milestone.

Outer whitespace is stripped from fields. NUL characters are rejected. Normal
CSV quoting supports commas and embedded newlines in merchant descriptions.
Missing, extra, or repeated headers and wrong column counts are errors. Blank
records are errors; a final line terminator is normal. A header-only file is a
valid empty import returning `inserted=0, skipped=0` for an existing account.

## Accounts, identity, and service usage

`Account` has a UUID, display name, and timezone-aware creation timestamp. Display
names are not unique. An account represents one bank/source account and source-ID
namespace: retain its UUID across imports. If two providers do not share stable
transaction IDs, use separate accounts or normalize their identities upstream.
Account creation is separate from CSV import; there is no account-management API.

```python
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from ledger.persistence.models import Account
from ledger.persistence.bank_ingest import import_bank_transactions

engine = create_engine(database_url)  # postgresql+psycopg://...
with Session(engine) as session, session.begin():
    account = Account(name="Checking")
    session.add(account)
    session.flush()
    account_id = account.id  # retain this UUID for every subsequent import

summary = import_bank_transactions(
    engine, account_id, Path("statement.csv").read_text(encoding="utf-8-sig")
)
print(summary.inserted, summary.skipped)
engine.dispose()
```

Database uniqueness is **`(account_id, source_transaction_id)`**, enforced by
`uq_bank_transactions_source_identity`. Different source IDs remain distinct even
when every other field matches. The same source ID can exist in different accounts.
An identical existing record is skipped without changing its UUID, timestamp, or
audit trail. Different normalized date, merchant, amount, or currency under the
same identity raises `TransactionIdentityConflict` with its CSV row and source ID;
the existing row is never overwritten.

Identical repeats within a single CSV are skipped too. Conflicting repeats within
the CSV fail validation before database access, identifying both row numbers.
`inserted + skipped` equals the number of valid input records on success, including
in-file repeats. On any failure there is no success summary or partial import.

## Validation and transaction boundaries

The complete file is parsed and validated **before opening a database connection**.
`CSVValidationError.errors` contains `RowError(row, message)` entries for all
detectable invalid records, with field-specific messages. Rows use physical line
numbers (the header is line 1); malformed CSV errors identify the line where the
parser detected the problem and parsing stops if it cannot recover.

After validation, the service owns one transaction using PostgreSQL's default
**READ COMMITTED** isolation. It checks that the account exists, then inserts all
unique records in source-ID order using `ON CONFLICT DO NOTHING RETURNING id`.
Consistent ordering avoids lock-order inversions between overlapping imports.
The unique constraint arbitrates simultaneous imports. A losing insert waits for
the competing transaction and reads its committed record in a fresh statement to
check content. If the competitor rolls back, the waiting insert can create it.
No internal retry loop is introduced.

Only newly inserted transactions receive `bank_transaction.created` audit events.
Events reference the transaction by foreign key and include an exact decimal
string, account UUID, source ID, date, merchant, and currency snapshot. All new
transactions and events commit together. A database conflict or audit-write
failure rolls back the entire batch, including rows already processed.

The shared `audit_events` table now permits either a receipt reference or a bank
transaction reference, with a check requiring exactly one. Creation-event types
must reference the corresponding entity type. Partial unique indexes enforce
at most one creation event per receipt or transaction. Existing receipt audit
data is retained. As with receipt ingestion, direct SQL writers must honor the
entity-plus-event contract; administrator-level audit immutability is not added.

Migration `0002_bank_ingestion` builds on the committed receipt migration.
Downgrading it removes accounts, bank transactions, and their audit rows while
retaining receipts and receipt audit history. It is a destructive rollback of
this feature's data, exercised only in isolated test schemas during testing.

## Tests

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres  # receipt and bank ingestion tests
make test           # complete suite, including extraction/evaluation tests
```

Tests migrate fresh random schemas and remove them afterward. Without
TEST_DATABASE_URL, database tests explicitly skip; `make test-postgres` requires
the variable. Configured connection failures are errors, not skips.

Coverage includes committed signed Decimal persistence and snapshots, repeat
imports, distinct payments, account scoping, identical/conflicting in-file
duplicates, row-level validation before database access, unknown accounts,
concurrent reversed-order imports, whole-batch rollback on identity conflicts and
audit failures, direct uniqueness/check constraints, the sample CSV, migration
agreement with models, and preservation of receipt history across migrations.
