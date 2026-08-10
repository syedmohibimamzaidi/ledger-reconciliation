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

def _group_into_rows(segments, tol_ratio: float = 0.6) -> list[str]:
    """Join OCR segments that share a physical row.

    Segments are grouped when their vertical centres fall within a fraction of
    the median segment height, then ordered left-to-right. Tolerance is relative
    rather than absolute because receipt scans vary in resolution.

    This is the whole reason bboxes are worth keeping: it turns
        "TOTAL"  /  "133.70"
    into
        "TOTAL  133.70"
    which is what any cue-based extractor needs to work at all.
    """
    if not segments:
        return []

    heights = sorted(s[3] - s[2] for s in segments)
    med_h = heights[len(heights) // 2] or 1
    tol = med_h * tol_ratio

    segments = sorted(segments, key=lambda s: ((s[2] + s[3]) / 2, s[1]))
    rows, current = [], [segments[0]]
    centre = (segments[0][2] + segments[0][3]) / 2

    for seg in segments[1:]:
        y = (seg[2] + seg[3]) / 2
        if abs(y - centre) <= tol:
            current.append(seg)
            centre = sum((s[2] + s[3]) / 2 for s in current) / len(current)
        else:
            rows.append(current)
            current, centre = [seg], y
    rows.append(current)

    return ["  ".join(s[0] for s in sorted(r, key=lambda s: s[1])) for r in rows]

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
            segments = []
            for line in box_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split(",", 8)
                if len(parts) != 9:
                    continue
                try:
                    xs = [float(parts[i]) for i in (0, 2, 4, 6)]
                    ys = [float(parts[i]) for i in (1, 3, 5, 7)]
                except ValueError:
                    continue
                text = parts[8].strip()
                if text:
                    segments.append((text, min(xs), min(ys), max(ys)))
            raw = "\n".join(_group_into_rows(segments))

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
