"""Job submission and claiming; no delivery recovery or automatic retries yet."""
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from ledger.persistence.ingest import IdentityConflict
from ledger.persistence.models import Account, ReconciliationJob
from ledger.persistence.reconcile import reconcile_account

DISPATCH_FAILURE = {"code": "dispatch_failed", "message": "Queue dispatch could not be confirmed; this job will not be retried automatically."}
EXECUTION_FAILURE = {"code": "reconciliation_failed", "message": "Reconciliation failed; no automatic retry is scheduled."}


class QueueUnavailable(RuntimeError):
    pass


def get_job(engine, job_id, account_id):
    with engine.connect() as connection:
        return connection.execute(select(ReconciliationJob.__table__).where(
            ReconciliationJob.id == job_id, ReconciliationJob.account_id == account_id)).mappings().one_or_none()


def submit_job(engine, queue, account_id: UUID, request_key: str):
    """Commit identity first. Only the winning insert attempts one Redis enqueue."""
    payload = {"operation": "reconcile_account", "version": 1, "account_id": str(account_id)}
    with engine.begin() as connection:
        # The unique request key serializes competing submissions.
        if connection.scalar(select(Account.id).where(Account.id == account_id)) is None:
            raise ValueError("account_id does not identify an existing account")
        job_id = connection.execute(insert(ReconciliationJob).values(
            account_id=account_id, request_key=request_key, request_payload=payload)
            .on_conflict_do_nothing(constraint="uq_reconciliation_job_request")
            .returning(ReconciliationJob.id)).scalar_one_or_none()
        if job_id is None:
            prior = connection.execute(select(ReconciliationJob.__table__).where(
                ReconciliationJob.request_key == request_key)).mappings().one()
            if prior["request_payload"] != payload:
                raise IdentityConflict("request_key already used with different reconciliation input")
            return dict(prior)
        if queue is None:
            # Roll back the new row, but permit existing-key reads without Redis.
            raise QueueUnavailable("Background reconciliation requires REDIS_URL")
    try:
        queue.enqueue("ledger.jobs.tasks.reconcile_job", str(job_id), job_id=str(job_id),
                      result_ttl=0, failure_ttl=86400)
    except Exception:
        # An enqueue error can be ambiguous. A worker may already have claimed it;
        # never overwrite running/terminal status. No raw exception text is stored.
        with engine.begin() as connection:
            connection.execute(update(ReconciliationJob).where(
                ReconciliationJob.id == job_id, ReconciliationJob.status == "queued").values(
                    status="failed", finished_at=func.now(), updated_at=func.now(), failure_details=DISPATCH_FAILURE))
    return dict(get_job(engine, job_id, account_id))


def execute_job(engine, job_id: UUID):
    """Claim once, call the unchanged service, then persist a safe outcome.

    Status and reconciliation commits are intentionally separate. A crash between
    them can leave running status even when reconciliation committed successfully.
    """
    with engine.begin() as connection:
        account_id = connection.execute(update(ReconciliationJob).where(
            ReconciliationJob.id == job_id, ReconciliationJob.status == "queued").values(
                status="running", attempt_count=ReconciliationJob.attempt_count + 1,
                started_at=func.now(), updated_at=func.now()).returning(ReconciliationJob.account_id)).scalar_one_or_none()
    if account_id is None:
        return  # Unknown, running, succeeded and failed jobs are never reprocessed.
    try:
        result = asdict(reconcile_account(engine, account_id))
    except Exception:
        with engine.begin() as connection:
            connection.execute(update(ReconciliationJob).where(
                ReconciliationJob.id == job_id, ReconciliationJob.status == "running").values(
                    status="failed", failure_details=EXECUTION_FAILURE, finished_at=func.now(), updated_at=func.now()))
        return
    with engine.begin() as connection:
        connection.execute(update(ReconciliationJob).where(
            ReconciliationJob.id == job_id, ReconciliationJob.status == "running").values(
                status="succeeded", result_summary=result, finished_at=func.now(), updated_at=func.now()))
