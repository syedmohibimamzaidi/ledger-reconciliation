# Safe reconciliation of stored records

The service reconciles stored receipt totals against debits in one explicitly
selected account. It preserves both ingestion contracts and the independent
extraction/evaluation pipeline. No human decision actions or match overrides are
introduced in this milestone.

## Local demonstration

```sh
make setup
docker compose up -d --wait postgres
export DATABASE_URL='postgresql+psycopg://ledger:ledger@localhost:55432/ledger'
make migrate
make demo-reconciliation
```

The script provisions a dedicated account and four receipts using stable source
identities, assigns the receipts to that account, imports four bank transactions,
and reconciles twice. Expected outcomes:

| Example | Outcome | Reason |
| --- | --- | --- |
| Harbor Books: receipt 20.00, debit -20.00 | matched | Clear unique exact pair |
| Cedar Cafe: receipt 12.30, debit -15.00 | needs_review | Amount difference of 2.70 |
| North Market: one receipt, two identical debits | needs_review | Multiple transactions for one receipt |
| Lantern Bakery: no bank candidate | unmatched | No plausible candidates |

The first run writes four decisions; the repeated run writes zero. Subsequent
script runs reuse the existing records and write zero decisions on both runs.
The dedicated demo account does not affect the receipt or CSV ingestion demos.

## Explicit receipt scope

New receipts remain **unassigned** (`receipts.account_id IS NULL`) after ingestion.
No account is inferred from merchant, currency, source string, or amount.
Call the scope service with the UUIDs returned by receipt ingestion:

```python
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account

assigned = assign_receipts_to_account(engine, account_id, receipt_ids)
summary = reconcile_account(engine, account_id)
```

`account_id` and each receipt ID must be UUID objects. The account and receipts
must exist. Scope assignment atomically associates previously unassigned receipts
with the account and writes one `receipt.scoped` event per newly assigned receipt.
Repeating assignment to the same account is a no-op. A receipt already belonging
to another account rejects the whole assignment; reassignment is deferred.

The reconciliation scope is **all receipts assigned to that account**, including
prior accepted matches, and all its stored bank transactions. It cannot be narrowed
to a selected subset of receipts in a run. Receipts from another account and
unassigned receipts cannot participate. Positive receipt totals are eligible for
matching; zero totals are recorded as unmatched with an explicit unsupported
amount reason. The ingestion `pending` status is independent of reconciliation
and remains unchanged; reconciliation status lives in its own table.

## Conservative matching rules (conservative-v1)

A plausible receipt/transaction edge requires all of:

1. The receipt total is positive and the bank amount is negative. Credits,
   refunds, zero bank amounts, and split-payment aggregation are deferred.
2. Currency codes are identical. There is no conversion or cross-currency match.
3. The transaction date is **0 through 4 calendar days after** the receipt date,
   inclusive. Earlier dates and longer settlement delays are excluded.
4. Merchant keys agree. Normalize Unicode using NFKC, uppercase, strip outer
   whitespace, and remove one recognized leading processor prefix (`SQ *`,
   `TST*`, `PP*`, `SP `, `PY *`, or `IC*`, matching the baseline conventions).
   Punctuation separates tokens and whitespace is folded. Both the baseline
   `normalize_merchant` key and the full nonempty letter/digit token sequence must
   agree. The second check preserves store numbers, city names, corporate suffixes,
   single-character tokens, and Unicode letters that the baseline can discard.
   Thus `SQ *BLUE-BOTTLE` matches `Blue Bottle`, while `Store #123` and `Store #456`
   do not. Truncations, fuzzy similarity, transliteration, and unrecognized
   merchant aliases do not establish a plausible edge in this version.

**Amount does not filter plausible candidates.** The service compares exact
Decimal values and accepts only `receipt.amount == -transaction.amount`, with
no tolerance, float conversion, tip adjustment, or score threshold. A matching
merchant/date/currency debit of any different amount remains review evidence,
including differences outside the baseline matcher's 25% amount band.

Automatic acceptance requires exactly **one** plausible transaction for the
receipt and exactly **one** plausible receipt for that transaction, plus exact
amount equality. Ambiguity is measured on the entire plausible graph before any
assignment. An exact-amount edge with a different-amount competitor is still
ambiguous. No greedy tie breaking or "best score" booking is performed.

| Reason code | Meaning |
| --- | --- |
| `clear_unique_exact` | Accepted one-to-one exact pair |
| `amount_difference` | At least one plausible debit differs from the receipt total |
| `multiple_transactions_for_receipt` | More than one plausible bank candidate |
| `multiple_receipts_for_transaction` | A bank candidate has competing receipts |
| `transaction_already_matched` | A candidate is already accepted for another receipt |
| `no_plausible_candidates` | No edge passes merchant/date/currency/direction rules |
| `unsupported_receipt_amount` | Nonpositive receipt total cannot be matched |

Review outcomes may include multiple reason codes. Their candidate details contain
merchant/date/currency/amount snapshots, signed difference (`-bank_amount - total`),
date lag, and competing receipt UUIDs. Excluded transactions are not review
candidates. Transactions with no accepted link remain unmatched; plausible review
candidates are identifiable through receipt decision details. This milestone stores
outcomes per receipt, without a separate bank processing status.

## Persistence, idempotency, and concurrency

Migration `0003_reconciliation` adds nullable receipt account membership and
`reconciliation_decisions`. Each receipt has one current decision with status
`matched`, `needs_review`, or `unmatched`, a revision, details JSONB, and timestamps.
Only matched decisions have a non-null bank transaction reference.

Database guarantees include:

- Unique receipt and bank transaction references prevent either entity appearing
  in multiple accepted matches, even for direct SQL writes.
- Composite foreign keys require the decision receipt and bank transaction to
  belong to the same account. Their parent `(id, account_id)` pairs are unique.
- Status/link checks prevent review or unmatched decisions from claiming a bank
  transaction, and prevent accepted decisions without one.
- A unique partial audit index on receipt plus revision prevents duplicate
  `receipt.reconciliation_decided` events. Scope events are also unique per receipt.

`reconcile_account` owns one PostgreSQL **READ COMMITTED** transaction. It locks
the selected account row first, reads its receipts and bank transactions, computes
the graph, and writes changed decisions plus audit snapshots before committing.
Concurrent runs on the same account wait for that lock and then see the committed
outcomes. Scope assignment uses the same account lock and ordered receipt locks,
so scope cannot change partway through reconciliation. Different accounts operate
independently; receipt assignment cannot claim the same receipt for both.

An unchanged outcome and candidate snapshot writes nothing: no new revision,
timestamp change, or decision event. New bank data or newly assigned receipts can
change a review/unmatched outcome; that updates the current decision and appends
one new audited revision. Audit failure rolls back all new decisions and revisions
in the run. The service returns `matched`, `needs_review`, `unmatched`,
`unmatched_transactions`, and `decisions_written` counts only after commit.

Accepted matches are retained without automatic reopening. Their receipts and
transactions stay in the graph so subsequent competing records cannot become
artificially unique. Decisions reflect the committed bank records read after
acquiring the account lock; a bank import committed after that read participates
on the next run. There is no claim of uniqueness against future records. Audit
snapshots retain the evidence used when accepting a match. Human correction or
reopening of prior matches is deferred.

As with ingestion, application services honor the lock and entity/event contract.
The constraints enforce identity and scope, not merchant rules through direct SQL,
and do not provide administrator-level audit immutability.

Downgrading this migration removes scope assignments, current reconciliation
decisions, and their new audit event types. Existing receipts, accounts, bank
transactions, and ingestion audit events remain. Populated downgrade tests use
isolated schemas rather than the local demo database.

## Verification

```sh
export TEST_DATABASE_URL="$DATABASE_URL"
make test-postgres
make test
```

Pure unit tests exercise amount equality/differences, merchant safeguards, date
boundaries, currency/direction exclusions, and bidirectional ambiguity. PostgreSQL
tests cover persisted outcomes and evidence, scope isolation, repeated and
concurrent runs, new-data revisions, one-to-one and account constraints, concurrent
scope claims, whole-run/audit rollback, and populated migration compatibility.
Both ingestion suites and the extraction/evaluation tests remain part of the
full suite. Database tests skip without TEST_DATABASE_URL; configured database
failures are errors. `make test-postgres` requires the variable.
