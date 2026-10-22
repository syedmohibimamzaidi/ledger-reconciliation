"""Atomic ingestion with source-scoped idempotency under READ COMMITTED."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from .models import AuditEvent, Receipt


class IdentityConflict(ValueError):
    """The source identity already belongs to different receipt content."""


@dataclass(frozen=True)
class StructuredReceipt:
    merchant: str
    receipt_date: date
    amount: Decimal
    currency: str
    source: str
    source_receipt_id: str

    def validated_fields(self) -> dict:
        fields = {}
        for name, limit in (("merchant", 255), ("source", 100), ("source_receipt_id", 255)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or "\x00" in value:
                raise ValueError(f"{name} must be a nonblank string of at most {limit} characters")
            fields[name] = value.strip()
        if type(self.receipt_date) is not date:
            raise ValueError("receipt_date must be a datetime.date")
        amount = self.amount
        if (not isinstance(amount, Decimal) or not amount.is_finite()
                or amount < 0 or amount >= Decimal("10000000000000000")):
            raise ValueError("amount must be a finite, nonnegative Decimal fitting NUMERIC(18, 2)")
        # Reject fractional cents rather than silently round financial data.
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("amount must have at most two fractional decimal places")
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter currency code")
        return {**fields, "receipt_date": self.receipt_date,
                "amount": amount.quantize(Decimal("0.01")), "currency": currency}


@dataclass(frozen=True)
class IngestionResult:
    receipt_id: UUID
    created: bool


def ingest_receipt(engine: Engine, receipt: StructuredReceipt) -> IngestionResult:
    """Commit a receipt and creation event, or return its existing identity.

    Owns its transaction. The source must provide a stable ID per actual receipt,
    reusing it on retries. Changed content for that identity is an error.
    PostgreSQL's default READ COMMITTED isolation is required; no internal retries.
    """
    fields = receipt.validated_fields()
    if engine.dialect.name != "postgresql":
        raise ValueError("receipt ingestion requires PostgreSQL")
    with engine.begin() as connection:
        receipt_id = connection.execute(
            insert(Receipt).values(**fields)
            .on_conflict_do_nothing(constraint="uq_receipts_source_identity")
            .returning(Receipt.id)
        ).scalar_one_or_none()
        if receipt_id is None:
            # ON CONFLICT waits for an in-flight competing insert. A separate
            # SELECT gets a fresh READ COMMITTED snapshot after that commit.
            existing = connection.execute(select(Receipt.__table__).where(
                Receipt.source == fields["source"],
                Receipt.source_receipt_id == fields["source_receipt_id"],
            )).mappings().one()
            if any(existing[key] != value for key, value in fields.items()):
                raise IdentityConflict("source identity already exists with different receipt content")
            return IngestionResult(existing["id"], created=False)

        payload = {**fields, "receipt_date": fields["receipt_date"].isoformat(),
                   "amount": format(fields["amount"], ".2f"), "status": "pending"}
        connection.execute(insert(AuditEvent).values(
            receipt_id=receipt_id, event_type="receipt.created", payload=payload,
        ))
        return IngestionResult(receipt_id, created=True)
