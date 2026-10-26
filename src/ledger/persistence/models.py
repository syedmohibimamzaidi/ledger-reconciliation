"""Database models for receipt and account-scoped bank ingestion."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String,
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


class ReviewAction(Base):
    """Immutable human request/result and durable rejected candidate edges."""

    __tablename__ = "review_actions"
    __table_args__ = (
        UniqueConstraint("account_id", "request_id", name="uq_review_request"),
        UniqueConstraint("receipt_id", "resulting_revision", name="uq_review_case_revision"),
        ForeignKeyConstraint(["receipt_id", "account_id"], ["receipts.id", "receipts.account_id"],
                             name="fk_review_receipt_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["bank_transaction_id", "account_id"],
                             ["bank_transactions.id", "bank_transactions.account_id"],
                             name="fk_review_transaction_scope", ondelete="RESTRICT"),
        Index("uq_review_rejected_pair", "receipt_id", "bank_transaction_id", unique=True,
              postgresql_where=text("action = 'reject'")),
        Index("uq_review_confirmed_receipt", "receipt_id", unique=True,
              postgresql_where=text("action = 'confirm'")),
        CheckConstraint("action IN ('confirm', 'reject')", name="ck_review_action"),
        CheckConstraint("expected_revision > 0 AND resulting_revision = expected_revision + 1", name="ck_review_revision"),
        CheckConstraint("btrim(reviewer) <> '' AND btrim(reason) <> ''", name="ck_review_identity_reason"),
        CheckConstraint("receipt_amount > 0 AND bank_amount < 0 AND "
                        "receipt_amount <> 'NaN'::numeric AND bank_amount <> 'NaN'::numeric", name="ck_review_amounts"),
        CheckConstraint("action <> 'confirm' OR receipt_amount = -bank_amount OR acknowledge_amount_mismatch",
                        name="ck_review_mismatch_acknowledged"),
        CheckConstraint("(action = 'confirm' AND resulting_status = 'matched') OR "
                        "(action = 'reject' AND resulting_status IN ('needs_review', 'unmatched') "
                        "AND NOT acknowledge_amount_mismatch)", name="ck_review_result"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    account_id: Mapped[UUID] = mapped_column(Uuid)
    receipt_id: Mapped[UUID] = mapped_column(Uuid)
    bank_transaction_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(2000))
    acknowledge_amount_mismatch: Mapped[bool] = mapped_column(Boolean)
    expected_revision: Mapped[int] = mapped_column(Integer)
    resulting_revision: Mapped[int] = mapped_column(Integer)
    resulting_status: Mapped[str] = mapped_column(String(32))
    receipt_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    bank_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("review_action_id", name="uq_audit_review_action"),
        CheckConstraint("(event_type = 'receipt.reviewed') = (review_action_id IS NOT NULL)",
                        name="ck_audit_review_action"),
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
    review_action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("review_actions.id", name="fk_audit_review_action", ondelete="RESTRICT"),
    )
    receipt_id: Mapped[UUID | None] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), index=True)
    bank_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bank_transactions.id", name="fk_audit_bank_transaction", ondelete="RESTRICT"), index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconciliationJob(Base):
    """Durable dispatch intent and fenced execution status, authoritative over Redis."""

    __tablename__ = "reconciliation_jobs"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_reconciliation_job_request"),
        CheckConstraint("btrim(request_key) <> ''", name="ck_reconciliation_job_key"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 0 AND max_attempts",
                        name="ck_reconciliation_job_attempts"),
        CheckConstraint(
            "(status = 'queued' AND attempt_count < max_attempts AND finished_at IS NULL "
            "AND result_summary IS NULL AND attempt_token IS NULL AND lease_expires_at IS NULL "
            "AND next_attempt_at IS NOT NULL AND dispatch_after IS NOT NULL) OR "
            "(status = 'running' AND attempt_count > 0 AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND result_summary IS NULL AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND next_attempt_at IS NULL AND dispatch_after IS NULL) OR "
            "(status = 'succeeded' AND attempt_count > 0 AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND result_summary IS NOT NULL AND failure_details IS NULL AND attempt_token IS NULL "
            "AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND dispatch_after IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND result_summary IS NULL AND failure_details IS NOT NULL "
            "AND attempt_token IS NULL AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND dispatch_after IS NULL)",
            name="ck_reconciliation_job_state",
        ),
        Index("ix_reconciliation_jobs_dispatch", "status", "dispatch_after"),
        Index("ix_reconciliation_jobs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    request_key: Mapped[str] = mapped_column(String(255))
    request_payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    failure_details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dispatch_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempt_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
