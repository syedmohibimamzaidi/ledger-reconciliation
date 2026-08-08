# Ledger — Receipt Reconciliation Engine

Matches uploaded receipts to bank transactions, scores its own confidence, and
routes what it isn't sure about to a human. Built to be measured, not demoed.

**Status: Phase 0 — measurement first.** No model, no API, no UI yet. This phase
builds the evaluation harness and a rules baseline, so that every change after
it is falsifiable. The numbers below are deliberately mediocre; they are the
floor everything else has to beat.

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
of a claim here is *"matching precision **under a stated corruption model**"* —
not "94% matching accuracy". Anyone who thinks a parameter is unrealistic can
change it and re-run.

Extraction numbers on SROIE carry no such asterisk: real receipts, real
annotations, published protocol.

| Component | Ground truth | Externally verifiable |
|---|---|---|
| Header extraction | SROIE (ICDAR 2019 Task 3) | Yes |
| Line items | CORD v2 | Yes (Phase 1b) |
| Merchant normalisation | Derived from SROIE company field | Partially |
| Receipt ↔ transaction matching | Synthetic under stated corruption | **No** — read above |

---

## Current results

Synthetic corpus, dev split, rules baseline. **These are smoke-test numbers, not
results** — the synthetic generator uses the same templates the regex was
written against, so extraction here is self-graded by construction. Real
extraction numbers arrive with SROIE in Phase 1.

```
EXTRACTION (normalised)      P       R      F1   support
  vendor                 0.606   0.606   0.606        94
  date                   1.000   1.000   1.000        94
  total                  1.000   1.000   1.000        94

MATCHING
  blocking recall        1.000   (ceiling: 83/83 pairs reachable)
  precision              1.000
  recall                 0.711
  F1                     0.831
  wrong partner              0   (booked against the wrong charge)
  oracle-extraction F1   0.982   (extraction error costs 0.151 F1 downstream)
```

The vendor score is the interesting one: the baseline takes the first
substantive line, which fails on receipts whose header is wrapped in `***`.
That's a real error class, it's in the taxonomy, and Phase 1 has to beat it.

The `oracle-extraction` row is the number worth keeping. Running the same
matcher on perfect headers scores 0.982; on predicted headers, 0.831. **Errors
compound across stages**, and any evaluation that feeds gold data into stage two
is measuring a system that doesn't exist.

---

## Quick start

```bash
make setup
make test          # 12 tests, none of which check whether the score is good
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

- **Phase 0 — measurement** *(done)*: frozen splits, corruption model, baseline, harness
- **Phase 1 — extraction**: SROIE header fields, learned model vs rules, error taxonomy; CORD line items reported separately
- **Phase 2 — matching**: merchant normalisation as its own component, learned pair scorer, calibration curve, automation-rate/precision tradeoff
- **Phase 3 — backend**: FastAPI + Postgres + Redis workers, idempotency by content hash, bounded retries, dead-letter, append-only audit trail
- **Phase 4 — review queue**: side-by-side review UI, reviewer decisions captured as labelled data
- **Phase 5 — deploy**: hosted demo, architecture diagram, cost per document, failure analysis

## Deliberately out of scope

Plaid, subscription detection, spending insights, general anomaly detection,
expense-policy engines, multi-tenancy, real auth, LLM financial advice. Each is
a project of its own; adding them would make this one shallower, not deeper.
