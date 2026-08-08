"""Core domain types.

Deliberately plain dataclasses, not ORM models. Phase 0 is about measurement,
not persistence; Postgres models arrive in Phase 3 and will map onto these.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

HEADER_FIELDS = ("vendor", "date", "total")


@dataclass(frozen=True)
class LineItem:
    name: str
    qty: Optional[float] = None
    unit_price: Optional[float] = None
    price: Optional[float] = None


@dataclass
class Receipt:
    """Ground-truth or predicted receipt content.

    `raw_text` is the OCR text as provided by the corpus. We do NOT run our own
    OCR in Phase 0 - using the corpus-provided text isolates extraction quality
    from OCR quality, which is a different problem with a different owner.
    """

    receipt_id: str
    raw_text: str = ""
    vendor: Optional[str] = None
    date: Optional[date] = None
    total: Optional[float] = None
    line_items: list[LineItem] = field(default_factory=list)
    # Provenance: which corpus and split this came from.
    source: str = "unknown"

    def header(self) -> dict:
        return {"vendor": self.vendor, "date": self.date, "total": self.total}


@dataclass
class Transaction:
    """A card/bank transaction as it would arrive from a statement feed.

    `descriptor` is the mangled merchant string a real processor emits, e.g.
    "SQ *BLUE BOTTLE 4412  OAKLAND CA". `posted_date` lags the receipt date by
    settlement time. `amount` may differ from the receipt total (tip, refund).
    """

    txn_id: str
    descriptor: str
    posted_date: date
    amount: float
    # Only populated for synthetic ledgers - never visible to the matcher.
    _origin_receipt_id: Optional[str] = None


@dataclass(frozen=True)
class MatchPair:
    """A claimed or true (receipt, transaction) correspondence."""

    receipt_id: str
    txn_id: str
    confidence: float = 1.0

    def key(self) -> tuple[str, str]:
        return (self.receipt_id, self.txn_id)


@dataclass
class Dataset:
    """Everything an eval run needs, in one object."""

    receipts: list[Receipt]
    transactions: list[Transaction]
    gold_pairs: list[MatchPair]
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        matched_receipts = {p.receipt_id for p in self.gold_pairs}
        matched_txns = {p.txn_id for p in self.gold_pairs}
        return {
            "n_receipts": len(self.receipts),
            "n_transactions": len(self.transactions),
            "n_gold_pairs": len(self.gold_pairs),
            "orphan_receipts": len(self.receipts) - len(matched_receipts),
            "orphan_transactions": len(self.transactions) - len(matched_txns),
            **self.meta,
        }


def to_jsonable(obj):
    """Recursively convert dataclasses/dates to JSON-safe primitives."""
    if hasattr(obj, "__dataclass_fields__"):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
