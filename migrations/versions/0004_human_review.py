"""Idempotent human review actions and per-candidate rejections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_human_review"
down_revision = "0003_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "review_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reviewer", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("acknowledge_amount_mismatch", sa.Boolean(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_status", sa.String(32), nullable=False),
        sa.Column("receipt_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("bank_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "request_id", name="uq_review_request"),
        sa.UniqueConstraint("receipt_id", "resulting_revision", name="uq_review_case_revision"),
        sa.ForeignKeyConstraint(["receipt_id", "account_id"], ["receipts.id", "receipts.account_id"],
                                name="fk_review_receipt_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bank_transaction_id", "account_id"],
                                ["bank_transactions.id", "bank_transactions.account_id"],
                                name="fk_review_transaction_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("action IN ('confirm', 'reject')", name="ck_review_action"),
        sa.CheckConstraint("expected_revision > 0 AND resulting_revision = expected_revision + 1", name="ck_review_revision"),
        sa.CheckConstraint("btrim(reviewer) <> '' AND btrim(reason) <> ''", name="ck_review_identity_reason"),
        sa.CheckConstraint("receipt_amount > 0 AND bank_amount < 0 AND "
                           "receipt_amount <> 'NaN'::numeric AND bank_amount <> 'NaN'::numeric", name="ck_review_amounts"),
        sa.CheckConstraint("action <> 'confirm' OR receipt_amount = -bank_amount OR acknowledge_amount_mismatch",
                           name="ck_review_mismatch_acknowledged"),
        sa.CheckConstraint("(action = 'confirm' AND resulting_status = 'matched') OR "
                           "(action = 'reject' AND resulting_status IN ('needs_review', 'unmatched') "
                           "AND NOT acknowledge_amount_mismatch)", name="ck_review_result"),
    )
    op.create_index("uq_review_rejected_pair", "review_actions", ["receipt_id", "bank_transaction_id"],
                    unique=True, postgresql_where=sa.text("action = 'reject'"))
    op.create_index("uq_review_confirmed_receipt", "review_actions", ["receipt_id"],
                    unique=True, postgresql_where=sa.text("action = 'confirm'"))
    op.add_column("audit_events", sa.Column("review_action_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_audit_review_action", "audit_events", "review_actions", ["review_action_id"], ["id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_audit_review_action", "audit_events", ["review_action_id"])
    op.create_check_constraint("ck_audit_review_action", "audit_events",
                               "(event_type = 'receipt.reviewed') = (review_action_id IS NOT NULL)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM review_actions)")).scalar():
        raise RuntimeError("Cannot downgrade populated human review: accepted actions and rejection history must be retained")
    op.drop_constraint("ck_audit_review_action", "audit_events", type_="check")
    op.drop_constraint("uq_audit_review_action", "audit_events", type_="unique")
    op.drop_constraint("fk_audit_review_action", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "review_action_id")
    op.drop_table("review_actions")
