"""Database models for structured receipt ingestion."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String,
    UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("source", "source_receipt_id", name="uq_receipts_source_identity"),
        CheckConstraint("btrim(merchant) <> ''", name="ck_receipts_merchant"),
        CheckConstraint("btrim(source) <> '' AND btrim(source_receipt_id) <> ''",
                        name="ck_receipts_source_identity"),
        CheckConstraint("amount >= 0 AND amount <> 'NaN'::numeric", name="ck_receipts_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_receipts_currency"),
        CheckConstraint("status = 'pending'", name="ck_receipts_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    merchant: Mapped[str] = mapped_column(String(255))
    receipt_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(100))
    source_receipt_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("uq_audit_receipt_created", "receipt_id", unique=True,
              postgresql_where=text("event_type = 'receipt.created'")),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
