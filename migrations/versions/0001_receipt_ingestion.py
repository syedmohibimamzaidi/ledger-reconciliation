"""Structured receipts and their creation audit events."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_receipt_ingestion"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant", sa.String(255), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_receipt_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_receipt_id", name="uq_receipts_source_identity"),
        sa.CheckConstraint("btrim(merchant) <> ''", name="ck_receipts_merchant"),
        sa.CheckConstraint("btrim(source) <> '' AND btrim(source_receipt_id) <> ''", name="ck_receipts_source_identity"),
        sa.CheckConstraint("amount >= 0 AND amount <> 'NaN'::numeric", name="ck_receipts_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_receipts_currency"),
        sa.CheckConstraint("status = 'pending'", name="ck_receipts_status"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_audit_events_receipt_id", "audit_events", ["receipt_id"])
    op.create_index("uq_audit_receipt_created", "audit_events", ["receipt_id"], unique=True,
                    postgresql_where=sa.text("event_type = 'receipt.created'"))


def downgrade():
    op.drop_table("audit_events")
    op.drop_table("receipts")
