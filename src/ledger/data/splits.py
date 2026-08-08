"""Splitting, and the discipline around it.

Two rules this module exists to enforce:

1. GROUPED, NOT RANDOM. SROIE receipts from the same company share a printed
   template. A random receipt-level split puts the same template in train and
   test, so a model can memorise layout and post an inflated score. Published
   work on SROIE notes exactly this overlap. We group by vendor.

2. FROZEN. The split is written to data/splits/<name>.json and committed. It is
   generated once. If the file exists, we load it; we never silently re-derive
   it, because a split that moves when the code moves is not a held-out set.

Test is opened once, at the end of a phase, and the number goes in the results
file whatever it says. Dev is what you iterate against.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..schema import Receipt

SPLIT_DIR = Path("data/splits")


def _vendor_key(r: Receipt) -> str:
    """Coarse vendor identity, so 'LOBLAWS #1' and 'LOBLAWS' group together."""
    v = (r.vendor or "UNKNOWN").upper()
    v = re.sub(r"[^A-Z ]", " ", v)
    v = re.sub(r"\b(SDN|BHD|INC|LTD|LLC|CO|CORP|ENTERPRISE|TRADING)\b", " ", v)
    return re.sub(r"\s+", " ", v).strip()[:18] or "UNKNOWN"


def _bucket(key: str, salt: str) -> float:
    h = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def make(
    receipts: list[Receipt],
    name: str,
    ratios: tuple[float, float, float] = (0.70, 0.15, 0.15),
    salt: str = "ledger-v1",
) -> dict[str, list[str]]:
    """Deterministically assign whole vendor groups to train/dev/test."""
    train_r, dev_r, _ = ratios
    groups: dict[str, list[str]] = {}
    for r in receipts:
        groups.setdefault(_vendor_key(r), []).append(r.receipt_id)

    split: dict[str, list[str]] = {"train": [], "dev": [], "test": []}
    for key, ids in sorted(groups.items()):
        b = _bucket(key, salt)
        target = "train" if b < train_r else ("dev" if b < train_r + dev_r else "test")
        split[target].extend(sorted(ids))
    return split


def load_or_create(receipts: list[Receipt], name: str, **kw) -> dict[str, list[str]]:
    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    path = SPLIT_DIR / f"{name}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        known = {r.receipt_id for r in receipts}
        assigned = {i for ids in saved["splits"].values() for i in ids}
        missing = known - assigned
        if missing:
            raise RuntimeError(
                f"{len(missing)} receipts are not in the frozen split {path}.\n"
                "The corpus changed after the split was frozen. Do not regenerate "
                "silently - either restore the corpus or create a new named split."
            )
        return saved["splits"]

    splits = make(receipts, name, **kw)
    path.write_text(json.dumps(
        {
            "name": name,
            "grouping": "vendor",
            "salt": kw.get("salt", "ledger-v1"),
            "n_receipts": len(receipts),
            "sizes": {k: len(v) for k, v in splits.items()},
            "splits": splits,
        },
        indent=2,
    ))
    return splits


def subset(receipts: list[Receipt], ids: list[str]) -> list[Receipt]:
    want = set(ids)
    return [r for r in receipts if r.receipt_id in want]
