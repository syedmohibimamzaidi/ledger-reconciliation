"""Classify every `total` extraction failure into a named cause.

Reading 20 of 57 failures by eye gives you an impression, not a taxonomy - the
counts are what tell you which fix moves the number, and an impression can't
be recounted after you change the extractor.

So: test a hypothesis per failure, in priority order, and count. Then read two
examples from each bucket to confirm the rule is catching what its name claims.
Machine proposes, you verify.

The most important line in the output is the unreachable count - failures where
the gold total does not appear in the OCR text at all. Those cap what any
extractor can achieve, and chasing them is chasing noise.

    .venv/bin/python scripts/classify_total_errors.py
    .venv/bin/python scripts/classify_total_errors.py --examples 3
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ledger.data import sroie, splits as splits_mod  # noqa: E402
from ledger.baseline import extract as base_extract  # noqa: E402

CASH = re.compile(r"\b(CASH|TENDER|TENDERED|PAID|PAYMENT|VISA|MASTER|DEBIT|CARD)\b", re.I)
CHANGE = re.compile(r"\bCHANGE\b", re.I)
ROUND = re.compile(r"\b(ROUND|ADJUST|ADJ)\b", re.I)
SUBTOTAL = re.compile(r"\b(SUB\s*-?\s*TOTAL|SUBTOTAL|NETT?\b)", re.I)
TAXCUE = re.compile(r"\b(GST|SST|VAT|TAX|SERVICE CHARGE)\b", re.I)
TOTALCUE = re.compile(r"\b(TOTAL|AMOUNT DUE|GRAND TOTAL|AMT DUE|BALANCE)\b", re.I)


def money_on_line(line: str) -> list[float]:
    out = []
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})", line):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def lines_with(text: str, value: float) -> list[str]:
    return [ln for ln in text.splitlines() if value in money_on_line(ln)]


def classify(text: str, gold: float, pred: float | None) -> str:
    if pred is None:
        return "abstained (no amount found)"

    # Ceiling check first: if the right answer isn't in the text, no extraction
    # rule or model can reach it.
    if not lines_with(text, gold):
        return "unreachable (gold not in OCR text)"

    pred_lines = lines_with(text, pred)
    joined = " ".join(pred_lines)

    if CHANGE.search(joined):
        return "picked the CHANGE line"
    if CASH.search(joined) and not TOTALCUE.search(joined):
        return "picked cash tendered / card payment line"
    if ROUND.search(joined):
        return "picked a rounding adjustment line"
    if SUBTOTAL.search(joined) or (pred < gold and TAXCUE.search(text)):
        return "picked subtotal before tax"

    cued = [ln for ln in text.splitlines() if TOTALCUE.search(ln)]
    if not cued:
        return "no TOTAL cue on receipt (max() fallback)"
    if len(cued) > 1 and any(gold in money_on_line(ln) for ln in cued):
        return "multiple TOTAL cues, picked the wrong one"
    if pred > gold:
        return "overshoot, cause unclear"
    return "undershoot, cause unclear"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["train", "dev", "all"])
    ap.add_argument("--examples", type=int, default=2)
    args = ap.parse_args()

    receipts = sroie.load()
    split = splits_mod.load_or_create(receipts, "sroie-v1")
    if args.split != "all":
        receipts = splits_mod.subset(receipts, split[args.split])

    preds = {p.receipt_id: p for p in base_extract.extract_all(receipts)}

    buckets: dict[str, list] = defaultdict(list)
    scored = 0
    for r in receipts:
        if r.total is None:
            continue
        scored += 1
        p = preds[r.receipt_id].total
        if p is not None and round(p, 2) == round(r.total, 2):
            continue
        buckets[classify(r.raw_text, r.total, p)].append((r, p))

    failures = sum(len(v) for v in buckets.values())
    print(f"\nsroie/{args.split} · total · {failures} failures of {scored} scored\n")

    ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    for name, cases in ordered:
        pct = 100 * len(cases) / failures
        print(f"  {len(cases):>4}  ({pct:>4.1f}%)  {name}")

    unreachable = len(buckets.get("unreachable (gold not in OCR text)", []))
    ceiling = 1 - unreachable / scored
    print(f"\n  realistic ceiling for this field: {ceiling:.3f} F1")
    print(f"  ({unreachable} receipts have no recoverable answer in the text)\n")

    for name, cases in ordered:
        print("=" * 82)
        print(f"{name}  ({len(cases)})")
        for r, p in cases[: args.examples]:
            print(f"\n  {r.receipt_id}   gold={r.total}   pred={p}")
            for ln in r.raw_text.splitlines():
                if TOTALCUE.search(ln) or CASH.search(ln) or CHANGE.search(ln) \
                        or SUBTOTAL.search(ln) or ROUND.search(ln):
                    print(f"      | {ln.strip()[:74]}")
        print()

    print("Verify 2 examples per bucket before trusting the counts. Rename or")
    print("merge any bucket whose rule is not catching what its name claims.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())