"""Database models for receipt and account-scoped bank ingestion."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String,
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
        UniqueConstraint("id", "account_id", name="uq_receipts_id_account"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_receipts_account", ondelete="RESTRICT"), index=True,
    )
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
        UniqueConstraint("id", "account_id", name="uq_bank_transactions_id_account"),
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


class ReconciliationDecision(Base):
    """Current outcome per receipt; matched rows are the accepted matches."""

    __tablename__ = "reconciliation_decisions"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_reconciliation_receipt"),
        UniqueConstraint("bank_transaction_id", name="uq_reconciliation_transaction"),
        ForeignKeyConstraint(["receipt_id", "account_id"], ["receipts.id", "receipts.account_id"],
                             name="fk_reconciliation_receipt_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["bank_transaction_id", "account_id"],
                             ["bank_transactions.id", "bank_transactions.account_id"],
                             name="fk_reconciliation_transaction_scope", ondelete="RESTRICT"),
        CheckConstraint("status IN ('matched', 'needs_review', 'unmatched')", name="ck_reconciliation_status"),
        CheckConstraint("(status = 'matched') = (bank_transaction_id IS NOT NULL)", name="ck_reconciliation_match"),
        CheckConstraint("revision > 0", name="ck_reconciliation_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    receipt_id: Mapped[UUID] = mapped_column(Uuid)
    bank_transaction_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
        Index("uq_audit_receipt_scoped", "receipt_id", unique=True,
              postgresql_where=text("event_type = 'receipt.scoped'")),
        Index("uq_audit_receipt_decision_revision", "receipt_id", text("(payload ->> 'revision')"), unique=True,
              postgresql_where=text("event_type = 'receipt.reconciliation_decided'")),
        CheckConstraint("event_type <> 'receipt.reconciliation_decided' OR "
                        "(receipt_id IS NOT NULL AND payload ->> 'revision' IS NOT NULL AND "
                        "(payload ->> 'revision') ~ '^[1-9][0-9]*$')", name="ck_audit_decision_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID | None] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), index=True)
    bank_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bank_transactions.id", name="fk_audit_bank_transaction", ondelete="RESTRICT"), index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
