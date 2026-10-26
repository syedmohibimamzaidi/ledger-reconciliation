"""Durable dispatch timing, bounded attempts and fencing for recovery."""
from alembic import op
import sqlalchemy as sa

revision = "0006_job_recovery"
down_revision = "0005_reconciliation_jobs"
branch_labels = None
depends_on = None

STATE = (
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
    "AND attempt_token IS NULL AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND dispatch_after IS NULL)"
)


def upgrade():
    op.drop_constraint("ck_reconciliation_job_state", "reconciliation_jobs")
    op.drop_constraint("ck_reconciliation_job_attempts", "reconciliation_jobs")
    op.add_column("reconciliation_jobs", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("reconciliation_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("reconciliation_jobs", sa.Column("dispatch_after", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("reconciliation_jobs", sa.Column("attempt_token", sa.Uuid()))
    op.add_column("reconciliation_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    # Recover old unexecuted dispatch failures; preserve completed/permanent failures.
    op.execute("UPDATE reconciliation_jobs SET status='queued', finished_at=NULL, "
               "failure_details=jsonb_build_object('code','dispatch_failed',"
               "'message','Dispatch recovery scheduled.','retryable',true) "
               "WHERE status='failed' AND attempt_count=0 AND failure_details->>'code'='dispatch_failed'")
    op.execute("UPDATE reconciliation_jobs SET attempt_token=gen_random_uuid(), lease_expires_at=clock_timestamp() "
               "WHERE status='running'")
    op.execute("UPDATE reconciliation_jobs SET next_attempt_at=NULL, dispatch_after=NULL WHERE status<>'queued'")
    op.create_check_constraint("ck_reconciliation_job_attempts", "reconciliation_jobs",
                               "max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 0 AND max_attempts")
    op.create_check_constraint("ck_reconciliation_job_state", "reconciliation_jobs", STATE)
    op.create_index("ix_reconciliation_jobs_dispatch", "reconciliation_jobs", ["status", "dispatch_after"])
    op.create_index("ix_reconciliation_jobs_lease", "reconciliation_jobs", ["status", "lease_expires_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM reconciliation_jobs)")).scalar():
        raise RuntimeError("Cannot downgrade populated reconciliation jobs")
    op.drop_index("ix_reconciliation_jobs_dispatch", "reconciliation_jobs")
    op.drop_index("ix_reconciliation_jobs_lease", "reconciliation_jobs")
    op.drop_constraint("ck_reconciliation_job_state", "reconciliation_jobs")
    op.drop_constraint("ck_reconciliation_job_attempts", "reconciliation_jobs")
    for column in ("max_attempts", "next_attempt_at", "dispatch_after", "attempt_token", "lease_expires_at"):
        op.drop_column("reconciliation_jobs", column)
    op.create_check_constraint("ck_reconciliation_job_attempts", "reconciliation_jobs", "attempt_count BETWEEN 0 AND 1")
    op.create_check_constraint("ck_reconciliation_job_state", "reconciliation_jobs",
        "(status = 'queued' AND attempt_count = 0 AND started_at IS NULL AND finished_at IS NULL "
        "AND result_summary IS NULL AND failure_details IS NULL) OR "
        "(status = 'running' AND attempt_count = 1 AND started_at IS NOT NULL AND finished_at IS NULL "
        "AND result_summary IS NULL AND failure_details IS NULL) OR "
        "(status = 'succeeded' AND attempt_count = 1 AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND result_summary IS NOT NULL AND failure_details IS NULL) OR "
        "(status = 'failed' AND finished_at IS NOT NULL AND result_summary IS NULL AND failure_details IS NOT NULL "
        "AND ((attempt_count = 0 AND started_at IS NULL) OR (attempt_count = 1 AND started_at IS NOT NULL)))")
