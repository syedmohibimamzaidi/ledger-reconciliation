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
    # Day-first is the corpus norm (Malaysian receipts). Tried first, so an
    # ambiguous date like 05/06/2017 resolves to 5 June, not 6 May.
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y", "%d %b %y", "%d %b, %Y",
    "%d/%b/%Y", "%d-%b-%Y", "%d/%b/%y", "%d-%b-%y",
    "%b %d, %Y", "%B %d, %Y",
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    # Bare 8-digit. Order-independent in practice: 20180304 fails %d%m%Y
    # (month 18) and 25032018 fails %Y%m%d (month 20), so each only matches
    # its true reading.
    "%Y%m%d", "%d%m%Y",
    # Last resort. Only reached when day-first already failed, i.e. the first
    # component is > 12 as a month. Documented ambiguity, not a silent guess.
    "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
)


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip().upper()
    s = s.strip("()[]{}<>.,;: ")          # OCR wrappers: '(06/12/2016)'
    s = re.sub(r"^(DATE|DATED|TARIKH)\s*[:.]?\s*", "", s)
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
