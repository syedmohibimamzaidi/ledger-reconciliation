"""Database models for receipt and account-scoped bank ingestion."""

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


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_accounts_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "source_transaction_id", name="uq_bank_transactions_source_identity"),
        CheckConstraint("btrim(source_transaction_id) <> ''", name="ck_bank_transactions_source_id"),
        CheckConstraint("btrim(merchant) <> ''", name="ck_bank_transactions_merchant"),
        CheckConstraint("amount <> 'NaN'::numeric", name="ck_bank_transactions_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_bank_transactions_currency"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    source_transaction_id: Mapped[str] = mapped_column(String(255))
    transaction_date: Mapped[date] = mapped_column(Date)
    merchant: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("uq_audit_receipt_created", "receipt_id", unique=True,
              postgresql_where=text("event_type = 'receipt.created'")),
        Index("uq_audit_transaction_created", "bank_transaction_id", unique=True,
              postgresql_where=text("event_type = 'bank_transaction.created'")),
        CheckConstraint("(receipt_id IS NOT NULL) <> (bank_transaction_id IS NOT NULL)",
                        name="ck_audit_exactly_one_target"),
        CheckConstraint("(event_type <> 'receipt.created' OR receipt_id IS NOT NULL) AND "
                        "(event_type <> 'bank_transaction.created' OR bank_transaction_id IS NOT NULL)",
                        name="ck_audit_creation_target"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID | None] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), index=True)
    bank_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bank_transactions.id", name="fk_audit_bank_transaction", ondelete="RESTRICT"), index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
