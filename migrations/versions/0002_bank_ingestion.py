"""Accounts, bank transactions, and shared transaction audit events."""
from alembic import op
import sqlalchemy as sa

revision = "0002_bank_ingestion"
down_revision = "0001_receipt_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_accounts_name"),
    )
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("source_transaction_id", sa.String(255), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("merchant", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", "source_transaction_id", name="uq_bank_transactions_source_identity"),
        sa.CheckConstraint("btrim(source_transaction_id) <> ''", name="ck_bank_transactions_source_id"),
        sa.CheckConstraint("btrim(merchant) <> ''", name="ck_bank_transactions_merchant"),
        sa.CheckConstraint("amount <> 'NaN'::numeric", name="ck_bank_transactions_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_bank_transactions_currency"),
    )
    op.alter_column("audit_events", "receipt_id", nullable=True)
    op.add_column("audit_events", sa.Column("bank_transaction_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_audit_bank_transaction", "audit_events", "bank_transactions",
                          ["bank_transaction_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_audit_events_bank_transaction_id", "audit_events", ["bank_transaction_id"])
    op.create_index("uq_audit_transaction_created", "audit_events", ["bank_transaction_id"], unique=True,
                    postgresql_where=sa.text("event_type = 'bank_transaction.created'"))
    op.create_check_constraint("ck_audit_exactly_one_target", "audit_events",
                               "(receipt_id IS NOT NULL) <> (bank_transaction_id IS NOT NULL)")
    op.create_check_constraint("ck_audit_creation_target", "audit_events",
                               "(event_type <> 'receipt.created' OR receipt_id IS NOT NULL) AND "
                               "(event_type <> 'bank_transaction.created' OR bank_transaction_id IS NOT NULL)")


def downgrade():
    # Reversing this feature removes its data, including transaction audit rows.
    # Existing receipt rows and their audit history remain intact.
    op.execute("DELETE FROM audit_events WHERE bank_transaction_id IS NOT NULL")
    op.drop_constraint("ck_audit_creation_target", "audit_events", type_="check")
    op.drop_constraint("ck_audit_exactly_one_target", "audit_events", type_="check")
    op.drop_index("uq_audit_transaction_created", table_name="audit_events")
    op.drop_index("ix_audit_events_bank_transaction_id", table_name="audit_events")
    op.drop_constraint("fk_audit_bank_transaction", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "bank_transaction_id")
    op.alter_column("audit_events", "receipt_id", nullable=False)
    op.drop_table("bank_transactions")
    op.drop_table("accounts")
