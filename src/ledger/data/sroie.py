"""SROIE (ICDAR 2019 Task 3) loader.

Expected layout after running scripts/get_sroie.sh:

    data/sroie/
      img/        0325updated.task1train(626p)/*.jpg      (unused in Phase 0)
      box/        *.txt   OCR text + boxes, one line per detected string
      entities/   *.txt   JSON: {"company","date","address","total"}

The corpus gives us OCR output, so we do not run our own OCR. That keeps
extraction error separate from OCR error - two different problems, and mixing
them makes both unmeasurable.

Known corpus caveats, worth repeating in the README:
  * Malaysian receipts; date formats and address shapes are not Canadian.
  * The official test split shares most of its layouts with train, so a
    receipt-level random split leaks templates. See splits.by_vendor.
  * Annotations contain whitespace/comma noise that several papers patch by
    hand. We normalise rather than patch, and report both strict and
    normalised scores.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from ..schema import Receipt

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
    "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d %b, %Y",
)


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip().upper()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"[^\d.,\-]", "", str(s))
    s = s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def load(root: str | Path = "data/sroie") -> list[Receipt]:
    root = Path(root)
    ent_dir, box_dir = root / "entities", root / "box"
    if not ent_dir.exists():
        raise FileNotFoundError(
            f"{ent_dir} not found. Run scripts/get_sroie.sh, or use --corpus synthetic."
        )

    receipts: list[Receipt] = []
    for f in sorted(ent_dir.glob("*.txt")):
        try:
            ann = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue

        box_file = box_dir / f.name
        raw = ""
        if box_file.exists():
            # Box files are "x1,y1,...,x8,y8,text" - keep the text column only.
            lines = []
            for line in box_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split(",", 8)
                if len(parts) == 9:
                    lines.append(parts[8].strip())
            raw = "\n".join(lines)

        receipts.append(Receipt(
            receipt_id=f.stem,
            raw_text=raw,
            vendor=(ann.get("company") or "").strip() or None,
            date=parse_date(ann.get("date")),
            total=parse_amount(ann.get("total")),
            line_items=[],  # SROIE has no line items; CORD covers that in Phase 1b
            source="sroie",
        ))
    return receipts
