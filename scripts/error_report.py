"""Dump extraction failures with enough context to categorise them by hand.

This is the input to every phase, not a one-off. The workflow is: run it, read
the cases, write the taxonomy, THEN change the extractor. Changing code before
reading failures is how you end up fixing the third-most-common error class.

    .venv/bin/python scripts/error_report.py --field total --limit 20
    .venv/bin/python scripts/error_report.py --field vendor --split dev

Prints, per failure: the gold value, what the baseline predicted, and the lines
of raw text that mention either - so the cause is usually visible without
opening the receipt.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ledger.data import splits as splits_mod  # noqa: E402
from ledger.baseline import extract as base_extract  # noqa: E402
from ledger.eval.metrics import _norm  # noqa: E402


def load_corpus(name: str):
    if name == "synthetic":
        from ledger.data import synthetic
        return synthetic.generate()
    from ledger.data import sroie
    return sroie.load()


def relevant_lines(text: str, gold, pred, field: str) -> list[str]:
    """Lines mentioning the gold or predicted value, plus any cue keyword."""
    needles = []
    for v in (gold, pred):
        if v is None:
            continue
        s = f"{v:.2f}" if field == "total" else str(v)
        needles.append(s.upper())
        if field == "total":
            needles.append(s.replace(".", ","))
        if field == "date":
            needles.append(str(v).replace("-", "/"))

    cue = {
        "total": r"TOTAL|AMOUNT|DUE|CASH|CHANGE|ROUND|GST|NETT|PAID|BALANCE",
        "date": r"DATE|TARIKH|TIME",
        "vendor": r"",
    }[field]

    out = []
    for i, line in enumerate(text.splitlines()):
        u = line.upper()
        hit = any(n and n in u for n in needles) or (cue and re.search(cue, u))
        if hit:
            out.append(f"    {i:>3} | {line.strip()[:78]}")
    return out[:14]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="sroie", choices=["sroie", "synthetic"])
    ap.add_argument("--split", default="dev", choices=["train", "dev", "test", "all"])
    ap.add_argument("--field", default="total", choices=["total", "vendor", "date"])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--head", type=int, default=0,
                    help="also print the first N lines of raw text")
    args = ap.parse_args()

    if args.split == "test":
        print("Refusing to inspect the test split. Use dev.", file=sys.stderr)
        return 2

    receipts = load_corpus(args.corpus)
    split = splits_mod.load_or_create(receipts, f"{args.corpus}-v1")
    if args.split != "all":
        receipts = splits_mod.subset(receipts, split[args.split])

    preds = {p.receipt_id: p for p in base_extract.extract_all(receipts)}

    wrong, missing = [], []
    for r in receipts:
        gold = getattr(r, args.field)
        if gold is None:
            continue
        pred = getattr(preds[r.receipt_id], args.field)
        if _norm(args.field, pred, False) == _norm(args.field, gold, False):
            continue
        (missing if pred is None else wrong).append((r, gold, pred))

    total = len(receipts)
    print(f"\n{args.corpus}/{args.split} · field={args.field} · {total} receipts")
    print(f"{len(wrong)} wrong, {len(missing)} abstained "
          f"({len(wrong) + len(missing)} failures)\n")

    if args.field == "total" and wrong:
        # Cheap first cut at a taxonomy: is the prediction above or below gold,
        # and by how much? Overshoot usually means a cash-tendered or card line;
        # undershoot usually means subtotal-before-tax.
        buckets = Counter()
        for _, g, p in wrong:
            if p is None or not g:
                continue
            ratio = p / g
            if ratio > 1.5:
                buckets["pred >> gold (grabbed a much larger figure)"] += 1
            elif ratio > 1.0:
                buckets["pred > gold (cash tendered? card total?)"] += 1
            elif ratio > 0.85:
                buckets["pred slightly < gold (subtotal before tax/rounding?)"] += 1
            else:
                buckets["pred << gold (line item? change?)"] += 1
        print("  direction of error:")
        for k, n in buckets.most_common():
            print(f"    {n:>4}  {k}")
        print()

    shown = wrong[: args.limit]
    for r, gold, pred in shown:
        print("=" * 82)
        print(f"{r.receipt_id}   gold={gold!r}   pred={pred!r}")
        for ln in relevant_lines(r.raw_text, gold, pred, args.field):
            print(ln)
        if args.head:
            print("    --- head ---")
            for i, line in enumerate(r.raw_text.splitlines()[: args.head]):
                print(f"    {i:>3} | {line.strip()[:78]}")
        print()

    if missing:
        print("=" * 82)
        print(f"ABSTAINED ({len(missing)} - extractor returned None)")
        for r, gold, _ in missing[:10]:
            print(f"  {r.receipt_id:<24} gold={gold!r}")
        print()

    print("Now write the taxonomy: group these into 4-6 named causes with counts,")
    print("and record it before changing any extraction code.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())