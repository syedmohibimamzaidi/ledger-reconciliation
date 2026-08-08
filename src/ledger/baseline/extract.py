"""Rules baseline for header extraction.

This is the number every later model has to beat. Keep it here, keep it honest,
and keep reporting it: "my model got 0.89 F1" means nothing on its own, while
"regex got 0.71, the model got 0.89" is an actual result. On some fields the
rules will win, and that finding is worth more than a uniform sweep.
"""

from __future__ import annotations

import re
from datetime import date

from ..schema import Receipt
from ..data.sroie import parse_date, parse_amount

_DATE_RE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})\b"
)
_AMOUNT_RE = re.compile(r"(?:RM|CAD|USD|\$)?\s*(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})")
_TOTAL_CUE = re.compile(r"\b(TOTAL|AMOUNT DUE|GRAND TOTAL|AMT DUE|BALANCE)\b", re.I)
_NOISE_CUE = re.compile(r"\b(SUB\s*TOTAL|SUBTOTAL|TAX|GST|CHANGE|CASH|ROUND)\b", re.I)
_NON_VENDOR = re.compile(r"^\s*(\*+|-+|=+|TEL|NO\.|INVOICE|RECEIPT|\d)", re.I)


def extract_vendor(text: str) -> str | None:
    """First substantive line. Vendor names sit at the top of nearly every
    receipt, which is why this cheap rule is hard to beat by much."""
    for line in text.splitlines()[:6]:
        s = re.sub(r"[*=\-]+", " ", line).strip()
        if len(s) >= 3 and not _NON_VENDOR.match(line) and not s.isdigit():
            return re.sub(r"\s+", " ", s).upper()
    return None


def extract_date(text: str) -> date | None:
    for m in _DATE_RE.finditer(text):
        d = parse_date(m.group(1))
        if d and 2000 <= d.year <= 2030:
            return d
    return None


def extract_total(text: str) -> float | None:
    """Prefer an amount on a line cued by TOTAL; fall back to the max amount.

    The fallback is the interesting failure mode - on receipts where the card
    payment line prints a larger figure than the total, max() is wrong. Worth
    keeping in the error taxonomy.
    """
    cued: list[float] = []
    for line in text.splitlines():
        if _TOTAL_CUE.search(line) and not _NOISE_CUE.search(line):
            cued += [v for v in (parse_amount(m.group(1))
                                 for m in _AMOUNT_RE.finditer(line)) if v]
    if cued:
        return max(cued)
    allv = [v for v in (parse_amount(m.group(1))
                        for m in _AMOUNT_RE.finditer(text)) if v]
    return max(allv) if allv else None


def extract(receipt: Receipt) -> Receipt:
    """Predict header fields from raw text only. Never reads ground truth."""
    t = receipt.raw_text or ""
    return Receipt(
        receipt_id=receipt.receipt_id,
        raw_text="",
        vendor=extract_vendor(t),
        date=extract_date(t),
        total=extract_total(t),
        source=f"{receipt.source}:baseline",
    )


def extract_all(receipts: list[Receipt]) -> list[Receipt]:
    return [extract(r) for r in receipts]
