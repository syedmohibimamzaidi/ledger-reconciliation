# Ledger — Receipt Reconciliation Engine

Matches uploaded receipts to bank transactions, scores its own confidence, and
routes what it isn't sure about to a human. Built to be measured, not demoed.

**Status:** Phase 0 measurement pipeline is complete. The first backend slice
adds PostgreSQL-backed, idempotent structured receipt ingestion and atomic creation
audit events. No learned model, API, workers, or UI yet.

See [receipt ingestion](docs/receipt-ingestion.md) for the service contract,
local PostgreSQL demo, migrations, and integration tests.

---

## Read this before trusting any matching number

No public dataset pairs receipts with the bank transactions they settled to.
Receipt extraction can be measured against real labelled data (SROIE, ICDAR
2019); receipt→transaction matching cannot, because the ledger side doesn't
exist publicly.

So the transaction ledger here is **derived from receipt ground truth and then
degraded** by an explicit corruption model: settlement lag, tip and refund
drift, descriptor mangling by acquirer prefixes and truncation, duplicate
charges, orphan receipts, orphan transactions, and same-merchant near-duplicate
"confusers".

Every parameter lives in [`src/ledger/data/corruption.py`](src/ledger/data/corruption.py),
is versioned, and is written into every results file. That means the honest form
of a claim here is _"matching precision **under a stated corruption model**"_ —
not "94% matching accuracy". Anyone who thinks a parameter is unrealistic can
change it and re-run.

Extraction numbers on SROIE carry no such asterisk: real receipts, real
annotations, published protocol.

| Component                      | Ground truth                      | Externally verifiable |
| ------------------------------ | --------------------------------- | --------------------- |
| Header extraction              | SROIE (ICDAR 2019 Task 3)         | Yes                   |
| Line items                     | CORD v2                           | Yes (Phase 1b)        |
| Merchant normalisation         | Derived from SROIE company field  | Partially             |
| Receipt ↔ transaction matching | Synthetic under stated corruption | **No** — read above   |

---

## Current results

SROIE dev split (150 receipts), rules baseline. Extraction is measured against
real ICDAR 2019 annotations. Matching is measured under the corruption model
described above — read that section before quoting the matching numbers.

```
EXTRACTION                 P       R      F1   strict   support
  vendor               0.753   0.753   0.753    0.740       150
  date                 1.000   0.913   0.955    0.955       150
  total                0.617   0.617   0.617    0.617       149
  macro                                0.775    0.771

MATCHING (133 gold pairs, 192 transactions)
  blocking recall      0.774   ceiling: 103/133 pairs reachable
  candidates/receipt   2.57
  precision            0.731
  recall               0.511
  F1                   0.602
  wrong partner           22   receipt booked against the wrong charge
  spurious on orphan       3
  oracle-extraction F1 0.783   extraction error costs 0.181 F1 downstream
```

These are baseline numbers, not results to be proud of. They exist so that
every change in Phase 1 has something to beat.

### What the baseline gets wrong

**`total` at 0.617 is the root cause of the matching ceiling.** Candidate
generation builds its amount band from the _predicted_ total, so a wrong total
puts the true transaction outside the band and makes it unreachable — which is
most of why blocking recall is 0.774. Thirty gold pairs are lost before the
scorer runs, and no amount of matcher tuning recovers them. Fixing extraction
raises the matching ceiling for free.

**22 wrong-partner errors out of 133 pairs.** A receipt booked against the
wrong charge is a silent accounting error, unlike an unmatched receipt, which
is merely a queue item. This is the number that motivates confidence
thresholds and the review queue in Phase 2.

**Extraction error costs 0.181 F1 downstream.** The same matcher scores 0.783
on gold headers and 0.602 on predicted ones. Errors compound across stages, so
any evaluation that feeds gold data into stage two is measuring a system that
doesn't exist.

### A metric that was lying

The first SROIE run reported `date` F1 of **1.000** — on support of 136 out of 150. Fourteen receipts were silently excluded because the _gold annotation_
used a date format the parser couldn't read, and the scorer skips fields where
ground truth is unavailable. The perfect score was computed on the easy 91%.

Auditing the corpus (`scripts/audit_dates.py`) found 41 unparseable annotations
across nine formats: `27/MAR/2018`, `20180304` and `25032018` (bare 8-digit, in
opposite orders), `(06/12/2016)` with OCR parentheses, and `12/28/2017` in
US month-first order inside an otherwise day-first Malaysian corpus.

After the fix, support rose to 150 and date F1 **fell to 0.955**. Matching F1
fell too, from 0.654 to 0.602, because 14 harder gold pairs entered the
evaluation. Both drops are the metric becoming honest rather than the system
getting worse — and a score that improves when you fix a bug in the _data_
would be a score worth distrusting.

## Quick start

```bash
make setup
make test          # pipeline + ingestion tests; database tests need TEST_DATABASE_URL
make eval          # synthetic corpus, no download needed
make data          # fetch SROIE (~600 receipts)
make eval-sroie
```

---

## Design decisions

**Split by vendor, not by receipt.** SROIE receipts from the same company share
a printed template, and published work notes that most test layouts also appear
in train. A random receipt-level split therefore leaks templates and inflates
scores. Splits are grouped by normalised vendor and hashed deterministically.

**The split is frozen and committed.** Generated once, written to
`data/splits/`, and loaded thereafter. If the corpus changes, the loader raises
rather than silently re-deriving — a split that moves when the code moves isn't
a held-out set. Test is opened at the end of a phase, once, and whatever it says
gets committed.

**Extraction is scored strict and normalised.** Strict is exact string match,
the official SROIE protocol, comparable to published numbers. Normalised folds
case, whitespace and punctuation and parses dates and amounts as typed values —
closer to what the matcher actually consumes. Reporting only the normalised
score inflates; reporting only strict understates. The gap between them is
itself a result.

**Null is an abstention, not an error.** Precision and recall are tracked
separately per field, because a system that declines to guess when uncertain
should score differently from one that guesses wrong. That distinction is the
entire basis of the review queue, and plain accuracy erases it.

**Blocking recall is reported as a ceiling.** Candidate generation is scored
before scoring quality, because no matcher can exceed the recall of the
candidates it's given. Tuning a scorer while blocking recall is 0.90 is wasted
work.

**Wrong-partner errors are counted separately.** Booking a receipt against the
wrong charge is materially worse than leaving it unmatched — one is a silent
accounting error, the other is a queue item. A single F1 number hides this.

**Greedy assignment, for now.** One-to-one pairing is currently greedy by
descending score. Global assignment (Hungarian) is the obvious alternative, at
O(n³) for a likely small gain; Phase 2 measures whether it's worth it rather
than assuming.

---

## Layout

```
src/ledger/
  schema.py              Receipt, Transaction, MatchPair, Dataset
  data/
    corruption.py        the corruption model — read this first
    ledger_gen.py        receipts -> corrupted transactions + gold pairs
    sroie.py             SROIE Task 3 loader
    synthetic.py         fallback corpus so `make eval` works on a fresh clone
    splits.py            frozen, vendor-grouped splits
  baseline/
    extract.py           regex header extraction
    match.py             blocking + heuristic scoring + greedy assignment
  eval/
    metrics.py           field F1, matching P/R/F1, blocking recall, ECE/Brier
    run_eval.py          `make eval`
evals/                   committed results, one JSON per run
```

## Roadmap

- **Phase 0 — measurement** _(done)_: frozen splits, corruption model, baseline, harness
- **Phase 1 — extraction**: SROIE header fields, learned model vs rules, error taxonomy; CORD line items reported separately
- **Phase 2 — matching**: merchant normalisation as its own component, learned pair scorer, calibration curve, automation-rate/precision tradeoff
- **Phase 3 — backend** _(started)_: Postgres structured receipt ingestion with source-identity idempotency and atomic creation audit events is implemented; API, Redis workers, bounded retries, dead-letter, and further audit workflows remain
- **Phase 4 — review queue**: side-by-side review UI, reviewer decisions captured as labelled data
- **Phase 5 — deploy**: hosted demo, architecture diagram, cost per document, failure analysis

## Deliberately out of scope

Plaid, subscription detection, spending insights, general anomaly detection,
expense-policy engines, multi-tenancy, real auth, LLM financial advice. Each is
a project of its own; adding them would make this one shallower, not deeper.
