"""Persistent reconciliation job identity and execution status."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_reconciliation_jobs"
down_revision = "0004_human_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reconciliation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(255), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result_summary", postgresql.JSONB(none_as_null=True)),
        sa.Column("failure_details", postgresql.JSONB(none_as_null=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("request_key", name="uq_reconciliation_job_request"),
        sa.CheckConstraint("btrim(request_key) <> ''", name="ck_reconciliation_job_key"),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 1", name="ck_reconciliation_job_attempts"),
        sa.CheckConstraint(
            "(status = 'queued' AND attempt_count = 0 AND started_at IS NULL AND finished_at IS NULL "
            "AND result_summary IS NULL AND failure_details IS NULL) OR "
            "(status = 'running' AND attempt_count = 1 AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND result_summary IS NULL AND failure_details IS NULL) OR "
            "(status = 'succeeded' AND attempt_count = 1 AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND result_summary IS NOT NULL AND failure_details IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND result_summary IS NULL AND failure_details IS NOT NULL "
            "AND ((attempt_count = 0 AND started_at IS NULL) OR (attempt_count = 1 AND started_at IS NOT NULL)))",
            name="ck_reconciliation_job_state",
        ),
    )
    op.create_index("ix_reconciliation_jobs_account_id", "reconciliation_jobs", ["account_id"])


def downgrade():
    # Do not silently discard a durable execution record or a pending job.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM reconciliation_jobs)")).scalar():
        raise RuntimeError("Cannot downgrade populated reconciliation jobs")
    op.drop_table("reconciliation_jobs")
