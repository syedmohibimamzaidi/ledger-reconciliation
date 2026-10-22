"""Account receipt scope and one-to-one reconciliation outcomes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_reconciliation"
down_revision = "0002_bank_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("receipts", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_receipts_account", "receipts", "accounts", ["account_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_receipts_account_id", "receipts", ["account_id"])
    op.create_unique_constraint("uq_receipts_id_account", "receipts", ["id", "account_id"])
    op.create_unique_constraint("uq_bank_transactions_id_account", "bank_transactions", ["id", "account_id"])
    op.create_table(
        "reconciliation_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_id", name="uq_reconciliation_receipt"),
        sa.UniqueConstraint("bank_transaction_id", name="uq_reconciliation_transaction"),
        sa.ForeignKeyConstraint(["receipt_id", "account_id"], ["receipts.id", "receipts.account_id"],
                                name="fk_reconciliation_receipt_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bank_transaction_id", "account_id"],
                                ["bank_transactions.id", "bank_transactions.account_id"],
                                name="fk_reconciliation_transaction_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('matched', 'needs_review', 'unmatched')", name="ck_reconciliation_status"),
        sa.CheckConstraint("(status = 'matched') = (bank_transaction_id IS NOT NULL)", name="ck_reconciliation_match"),
        sa.CheckConstraint("revision > 0", name="ck_reconciliation_revision"),
    )
    op.create_index("ix_reconciliation_decisions_account_id", "reconciliation_decisions", ["account_id"])
    op.create_index("uq_audit_receipt_scoped", "audit_events", ["receipt_id"], unique=True,
                    postgresql_where=sa.text("event_type = 'receipt.scoped'"))
    op.create_index("uq_audit_receipt_decision_revision", "audit_events",
                    ["receipt_id", sa.text("(payload ->> 'revision')")], unique=True,
                    postgresql_where=sa.text("event_type = 'receipt.reconciliation_decided'"))
    op.create_check_constraint("ck_audit_decision_revision", "audit_events",
                               "event_type <> 'receipt.reconciliation_decided' OR "
                               "(receipt_id IS NOT NULL AND payload ->> 'revision' IS NOT NULL AND "
                               "(payload ->> 'revision') ~ '^[1-9][0-9]*$')")


def downgrade():
    op.execute("DELETE FROM audit_events WHERE event_type IN ('receipt.scoped', 'receipt.reconciliation_decided')")
    op.drop_constraint("ck_audit_decision_revision", "audit_events", type_="check")
    op.drop_index("uq_audit_receipt_decision_revision", table_name="audit_events")
    op.drop_index("uq_audit_receipt_scoped", table_name="audit_events")
    op.drop_table("reconciliation_decisions")
    op.drop_constraint("uq_bank_transactions_id_account", "bank_transactions", type_="unique")
    op.drop_constraint("uq_receipts_id_account", "receipts", type_="unique")
    op.drop_index("ix_receipts_account_id", table_name="receipts")
    op.drop_constraint("fk_receipts_account", "receipts", type_="foreignkey")
    op.drop_column("receipts", "account_id")
