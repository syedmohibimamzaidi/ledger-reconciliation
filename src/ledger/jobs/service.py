"""PostgreSQL owns dispatch intent, retry timing and fenced execution attempts."""
from dataclasses import asdict
from datetime import timedelta
from uuid import UUID, uuid4

from redis.exceptions import AuthenticationError, AuthorizationError, ConnectionError, TimeoutError
from rq.timeouts import JobTimeoutException
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as PoolTimeout

from ledger.persistence.ingest import IdentityConflict
from ledger.persistence.models import Account, ReconciliationJob as Job
from ledger.persistence.reconcile import reconcile_account_in_transaction

LEASE_SECONDS = 360
DISPATCH_SECONDS = 30
RETRY_SECONDS = 5


class QueueUnavailable(RuntimeError):
    pass


def _now(connection, now=None):
    return now if now is not None else connection.scalar(select(func.clock_timestamp()))


def get_job(engine, job_id, account_id):
    with engine.connect() as connection:
        return connection.execute(select(Job.__table__).where(
            Job.id == job_id, Job.account_id == account_id)).mappings().one_or_none()


def submit_job(engine, queue, account_id: UUID, request_key: str):
    """The job row is also its durable dispatch intent, committed before Redis."""
    payload = {"operation": "reconcile_account", "version": 1, "account_id": str(account_id)}
    with engine.begin() as connection:
        if connection.scalar(select(Account.id).where(Account.id == account_id)) is None:
            raise ValueError("account_id does not identify an existing account")
        job_id = connection.execute(insert(Job).values(
            account_id=account_id, request_key=request_key, request_payload=payload)
            .on_conflict_do_nothing(constraint="uq_reconciliation_job_request")
            .returning(Job.id)).scalar_one_or_none()
        if job_id is None:
            prior = connection.execute(select(Job.__table__).where(Job.request_key == request_key)).mappings().one()
            if prior["request_payload"] != payload:
                raise IdentityConflict("request_key already used with different reconciliation input")
            return dict(prior)
        if queue is None:
            raise QueueUnavailable("Background reconciliation requires REDIS_URL")
    # Optional immediate dispatch. A crash here is repaired by the dispatcher.
    dispatch_due(engine, queue, job_id=job_id, limit=1)
    return dict(get_job(engine, job_id, account_id))


def dispatch_due(engine, queue, *, limit=100, job_id=None, now=None):
    """Send bounded due intents; resend until claimed, even after Redis data loss.

    Enqueue and timestamp advancement share a short row-locking transaction. An
    ambiguous enqueue/commit can duplicate delivery, never erase durable intent.
    """
    sent = 0
    for _ in range(limit):
        with engine.begin() as connection:
            current = _now(connection, now)
            statement = select(Job.__table__).where(
                Job.status == "queued", Job.next_attempt_at <= current, Job.dispatch_after <= current)
            if job_id is not None:
                statement = statement.where(Job.id == job_id)
            row = connection.execute(statement.order_by(Job.dispatch_after, Job.id)
                                     .with_for_update(skip_locked=True).limit(1)).mappings().one_or_none()
            if row is None:
                break
            values = {"dispatch_after": current + timedelta(seconds=DISPATCH_SECONDS), "updated_at": current}
            try:
                # A fresh transport ID avoids overwriting a prior RQ message/result.
                queue.enqueue("ledger.jobs.tasks.reconcile_job", str(row["id"]),
                              job_id=str(uuid4()), result_ttl=0, failure_ttl=86400)
                sent += 1
            except Exception as error:
                transient = (isinstance(error, (ConnectionError, TimeoutError))
                             and not isinstance(error, (AuthenticationError, AuthorizationError)))
                values["failure_details"] = {"code": "dispatch_failed", "retryable": transient,
                    "message": "Queue dispatch could not be confirmed; durable dispatch will retry." if transient else
                               "Queue dispatch failed permanently; no retry scheduled."}
                if not transient:
                    values.update(status="failed", finished_at=current, dispatch_after=None, next_attempt_at=None)
            connection.execute(update(Job).where(Job.id == row["id"]).values(**values))
    return sent


def claim_attempt(engine, job_id, *, now=None):
    """Commit a bounded lease before executing. Duplicate messages cannot claim it."""
    with engine.begin() as connection:
        current = _now(connection, now)
        row = connection.execute(select(Job.__table__).where(
            Job.id == job_id, Job.status == "queued", Job.next_attempt_at <= current,
            Job.attempt_count < Job.max_attempts).with_for_update(skip_locked=True)).mappings().one_or_none()
        if row is None:
            return None
        token = uuid4()
        connection.execute(update(Job).where(Job.id == job_id).values(
            status="running", attempt_count=row["attempt_count"] + 1, attempt_token=token,
            lease_expires_at=current + timedelta(seconds=LEASE_SECONDS), next_attempt_at=None,
            dispatch_after=None, started_at=current, updated_at=current))
        return token


def is_transient(error):
    """Conservative allowlist; validation, constraints and unknown errors stop."""
    if isinstance(error, (JobTimeoutException, PoolTimeout)):
        return True
    if isinstance(error, DBAPIError):
        code = getattr(error.orig, "sqlstate", None)
        return (error.connection_invalidated or (isinstance(error, OperationalError) and code is None)
                or (code is not None and (code.startswith("08") or code in {
                    "40001", "40P01", "55P03", "57014", "53300", "57P01", "57P02", "57P03"})))
    return False


def _failure_values(row, current, code, retryable):
    retry = retryable and row["attempt_count"] < row["max_attempts"]
    failure = {"code": code if retry or not retryable else "retry_exhausted", "retryable": retry,
               "message": "Attempt interrupted; retry scheduled." if code == "worker_interrupted" and retry else
                          "Temporary reconciliation failure; retry scheduled." if retry else
                          "Reconciliation retry limit reached." if retryable else
                          "Reconciliation failed permanently; no retry scheduled."}
    due = current + timedelta(seconds=min(60, RETRY_SECONDS * 2 ** (row["attempt_count"] - 1))) if retry else None
    return {"status": "queued" if retry else "failed", "attempt_token": None, "lease_expires_at": None,
            "next_attempt_at": due, "dispatch_after": due, "updated_at": current,
            "finished_at": None if retry else current, "failure_details": failure}


def fail_attempt(engine, job_id, token, *, retryable, code, now=None):
    with engine.begin() as connection:
        row = connection.execute(select(Job.__table__).where(
            Job.id == job_id, Job.status == "running", Job.attempt_token == token)
            .with_for_update(skip_locked=True)).mappings().one_or_none()
        if row is None:
            return False
        connection.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token).values(
            **_failure_values(row, _now(connection, now), code, retryable)))
        return True


def run_attempt(engine, job_id, token):
    """Fence before any domain writes; commit success and reconciliation atomically."""
    try:
        with engine.begin() as connection:
            row = connection.execute(select(Job.__table__).where(
                Job.id == job_id, Job.status == "running", Job.attempt_token == token)
                .with_for_update(skip_locked=True)).mappings().one_or_none()
            if row is None or row["lease_expires_at"] <= _now(connection):
                return
            # The job lock fences recovery for the entire reconciliation transaction.
            connection.execute(text("SET LOCAL statement_timeout = '300s'"))
            result = asdict(reconcile_account_in_transaction(connection, row["account_id"]))
            connection.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token).values(
                status="succeeded", result_summary=result, failure_details=None,
                attempt_token=None, lease_expires_at=None, next_attempt_at=None, dispatch_after=None,
                finished_at=func.clock_timestamp(), updated_at=func.clock_timestamp()))
    except Exception as error:
        fail_attempt(engine, job_id, token, retryable=is_transient(error),
                     code="transient_failure" if is_transient(error) else "reconciliation_failed")


def execute_job(engine, job_id: UUID):
    token = claim_attempt(engine, job_id)
    if token is not None:
        run_attempt(engine, job_id, token)


def recover_expired(engine, *, limit=100, now=None):
    """Locked active transactions are skipped; abandoned leases consume an attempt."""
    recovered = 0
    with engine.begin() as connection:
        current = _now(connection, now)
        rows = connection.execute(select(Job.__table__).where(
            Job.status == "running", Job.lease_expires_at <= current)
            .order_by(Job.lease_expires_at, Job.id).with_for_update(skip_locked=True).limit(limit)).mappings().all()
        for row in rows:
            connection.execute(update(Job).where(Job.id == row["id"], Job.attempt_token == row["attempt_token"]).values(
                **_failure_values(row, current, "worker_interrupted", True)))
            recovered += 1
    return recovered
