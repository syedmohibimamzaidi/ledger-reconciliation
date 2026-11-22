"""Reconciliation cutoff for dashboard freshness and indexed job browsing."""
from alembic import op
import sqlalchemy as sa

revision = "0007_dashboard_reads"
down_revision = "0006_job_recovery"
branch_labels = None
depends_on = None


def upgrade():
    # Leave old accounts unknown until a real run, rather than inventing a cutoff.
    op.add_column("accounts", sa.Column("last_reconciled_at", sa.DateTime(timezone=True)))
    op.create_index("ix_reconciliation_jobs_account_created", "reconciliation_jobs",
                    ["account_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_reconciliation_jobs_account_created", "reconciliation_jobs")
    op.drop_column("accounts", "last_reconciled_at")
