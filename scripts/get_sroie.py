"""Fetch SROIE and write it into data/sroie/{entities,box}/.

Primary source is the HuggingFace mirror `jsdnrs/ICDAR2019-SROIE`, an extended
version of the ICDAR 2019 Task 3 release with 14 missing test annotations added
and some label corrections. The original competition site is unreliable; several
GitHub mirrors also carry the raw archive if this fails.

This script is deliberately defensive about column names - mirrors rename things
and the failure mode of guessing wrong is a corpus full of empty fields, which
would show up as a mysteriously low extraction score rather than an error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path("data/sroie")
ENTITY_KEYS = ("company", "date", "address", "total")


def pick(row: dict, *names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("pip install datasets", file=sys.stderr)
        return 1

    ds = load_dataset("jsdnrs/ICDAR2019-SROIE")
    (OUT / "entities").mkdir(parents=True, exist_ok=True)
    (OUT / "box").mkdir(parents=True, exist_ok=True)

    n = 0
    for split_name, split in ds.items():
        cols = split.column_names
        print(f"{split_name}: {len(split)} rows, columns={cols}")

        for i, row in enumerate(split):
            ann = pick(row, "entities", "annotation", "gt", "labels", "parse")
            if isinstance(ann, str):
                try:
                    ann = json.loads(ann)
                except json.JSONDecodeError:
                    ann = None
            if ann is None:
                ann = {k: row.get(k) for k in ENTITY_KEYS if k in row}
            if not ann or not any(ann.get(k) for k in ENTITY_KEYS):
                continue

            rid = str(pick(row, "id", "file_name", "image_id") or f"{split_name}_{i:05d}")
            rid = Path(rid).stem
            (OUT / "entities" / f"{rid}.txt").write_text(
                json.dumps({k: ann.get(k) for k in ENTITY_KEYS}), encoding="utf-8"
            )

            ocr = pick(row, "ocr", "text", "words", "box", "bbox_text")
            if isinstance(ocr, list):
                # Words list -> one line each; loader expects the 9th CSV column.
                ocr = "\n".join(
                    "0,0,0,0,0,0,0,0," + str(w if isinstance(w, str) else w.get("text", ""))
                    for w in ocr
                )
            elif isinstance(ocr, str) and "," not in ocr.split("\n")[0]:
                ocr = "\n".join("0,0,0,0,0,0,0,0," + ln for ln in ocr.splitlines())
            if ocr:
                (OUT / "box" / f"{rid}.txt").write_text(str(ocr), encoding="utf-8")
            n += 1

    print(f"\nwrote {n} receipts to {OUT}")
    if n == 0:
        print("No rows matched the expected schema. Inspect the columns printed "
              "above and extend pick() rather than guessing.", file=sys.stderr)
        return 2

    empty = sum(1 for f in (OUT / "entities").glob("*.txt")
                if not any(json.loads(f.read_text()).values()))
    if empty:
        print(f"WARNING: {empty} receipts have no populated fields.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
