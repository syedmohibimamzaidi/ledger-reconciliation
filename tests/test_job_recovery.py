"""Deterministic failure/recovery tests with real PostgreSQL, Redis and workers."""
from datetime import timedelta
import time
from uuid import UUID

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text, update

from ledger.jobs.service import (
    claim_attempt, dispatch_due, fail_attempt, get_job, recover_expired, run_attempt,
)
from ledger.persistence.models import AuditEvent, ReconciliationDecision, ReconciliationJob as Job
from test_jobs import account, client, finish_worker, queue, seed, start_worker, submit


def fail_audits(engine, count):
    # Sequence values survive rollbacks: exactly N worker attempts fail, across processes.
    with engine.begin() as connection:
        connection.execute(text("CREATE SEQUENCE failure_sequence"))
        connection.execute(text(f"""
            CREATE FUNCTION transient_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_type = 'receipt.reconciliation_decided' AND nextval('failure_sequence') <= {count} THEN
                    RAISE EXCEPTION 'private injected failure' USING ERRCODE = '40001';
                END IF;
                RETURN NEW;
            END $$;
        """))
        connection.execute(text("CREATE TRIGGER temporary_failure BEFORE INSERT ON audit_events "
                                "FOR EACH ROW EXECUTE FUNCTION transient_audit_failure()"))


def due(engine, job_id):
    with engine.begin() as connection:
        connection.execute(update(Job).where(Job.id == UUID(job_id), Job.status == "queued").values(
            next_attempt_at=func.clock_timestamp(), dispatch_after=func.clock_timestamp()))


def counts(engine):
    with engine.connect() as connection:
        return (connection.scalar(select(func.count()).select_from(ReconciliationDecision)),
                connection.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.event_type == "receipt.reconciliation_decided")))


def state(client, aid, job):
    response = client.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}")
    assert response.status_code == 200
    return response.json()


def test_transient_failure_retries_with_backoff_then_succeeds(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    fail_audits(db_engine, 1)
    job = submit(client, aid)
    finish_worker(start_worker(db_engine, queue))
    failed = state(client, aid, job)
    assert failed["status"] == "queued" and failed["attempt_count"] == 1
    assert failed["max_attempts"] == 3 and failed["failure_details"]["code"] == "transient_failure"
    assert failed["failure_details"]["retryable"] is True
    assert "private" not in str(failed)
    with db_engine.connect() as connection:
        row = get_job(db_engine, UUID(job["id"]), UUID(aid))
        assert row["next_attempt_at"] - row["updated_at"] == timedelta(seconds=5)
    assert counts(db_engine) == (0, 0)
    assert dispatch_due(db_engine, queue) == 0
    # Early duplicate delivery cannot bypass persistent retry timing.
    queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
    finish_worker(start_worker(db_engine, queue))
    assert state(client, aid, job) == failed
    due(db_engine, job["id"])
    assert dispatch_due(db_engine, queue) == 1
    finish_worker(start_worker(db_engine, queue))
    succeeded = state(client, aid, job)
    assert succeeded["status"] == "succeeded" and succeeded["attempt_count"] == 2
    assert succeeded["next_attempt_at"] is None and succeeded["lease_expires_at"] is None
    assert counts(db_engine) == (1, 1)
    # Completed redelivery changes neither job timestamps nor domain/audit rows.
    queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
    finish_worker(start_worker(db_engine, queue))
    assert counts(db_engine) == (1, 1) and state(client, aid, job) == succeeded


def test_retry_exhaustion_is_bounded(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    fail_audits(db_engine, 100)
    job = submit(client, aid)
    for attempt in (1, 2, 3):
        finish_worker(start_worker(db_engine, queue))
        row = get_job(db_engine, UUID(job["id"]), UUID(aid))
        assert row["attempt_count"] == attempt
        if attempt < 3:
            assert row["status"] == "queued"
            assert row["next_attempt_at"] - row["updated_at"] == timedelta(seconds=5 * 2 ** (attempt - 1))
            due(db_engine, job["id"])
            dispatch_due(db_engine, queue)
    terminal = state(client, aid, job)
    assert terminal["status"] == "failed" and terminal["failure_details"]["code"] == "retry_exhausted"
    assert terminal["failure_details"]["retryable"] is False
    assert terminal["next_attempt_at"] is None and terminal["finished_at"] is not None
    assert dispatch_due(db_engine, queue) == 0
    queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
    finish_worker(start_worker(db_engine, queue))
    assert state(client, aid, job) == terminal and counts(db_engine) == (0, 0)


def test_enqueue_failure_recovers_from_durable_intent(client, db_engine, queue, monkeypatch):
    aid = account(client)
    seed(client, aid)
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise RedisConnectionError("secret Redis URL")
        patch.setattr(queue, "enqueue", unavailable)
        job = submit(client, aid)
    assert job["status"] == "queued" and job["attempt_count"] == 0
    assert job["failure_details"]["code"] == "dispatch_failed"
    assert queue.count == 0
    due(db_engine, job["id"])
    dispatch_due(db_engine, queue)
    finish_worker(start_worker(db_engine, queue))
    assert state(client, aid, job)["status"] == "succeeded" and counts(db_engine) == (1, 1)


@pytest.mark.parametrize("mode", ["crash_before_enqueue", "ambiguous_enqueue", "redis_message_loss"])
def test_commit_enqueue_gap_and_redis_loss_are_recovered(client, db_engine, queue, monkeypatch, mode):
    aid = account(client)
    seed(client, aid)
    with monkeypatch.context() as patch:
        if mode == "crash_before_enqueue":
            # Simulate the process stopping after commit, before its optional fast dispatch.
            patch.setattr("ledger.jobs.service.dispatch_due", lambda *args, **kwargs: 0)
        elif mode == "ambiguous_enqueue":
            original = queue.enqueue
            def ambiguous(*args, **kwargs):
                original(*args, **kwargs)
                raise RedisConnectionError("response lost after enqueue")
            patch.setattr(queue, "enqueue", ambiguous)
        job = submit(client, aid)
    if mode == "redis_message_loss":
        queue.empty()  # Only this test's isolated queue, never FLUSHDB.
    due(db_engine, job["id"])
    dispatch_due(db_engine, queue)
    finish_worker(start_worker(db_engine, queue))
    final = state(client, aid, job)
    assert final["status"] == "succeeded" and final["attempt_count"] == 1
    assert counts(db_engine) == (1, 1)


def test_stale_attempt_cannot_execute_or_overwrite_newer_state(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    job = submit(client, aid)
    jid = UUID(job["id"])
    old = claim_attempt(db_engine, jid)
    with db_engine.begin() as connection:
        connection.execute(update(Job).where(Job.id == jid).values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'")))
    assert recover_expired(db_engine) == 1
    assert state(client, aid, job)["failure_details"]["code"] == "worker_interrupted"
    due(db_engine, job["id"])
    new = claim_attempt(db_engine, jid)
    assert old != new and new is not None
    run_attempt(db_engine, jid, old)
    assert not fail_attempt(db_engine, jid, old, retryable=False, code="reconciliation_failed")
    assert counts(db_engine) == (0, 0)
    assert get_job(db_engine, jid, UUID(aid))["attempt_token"] == new
    run_attempt(db_engine, jid, new)
    terminal = state(client, aid, job)
    assert terminal["status"] == "succeeded" and terminal["attempt_count"] == 2
    run_attempt(db_engine, jid, old)
    assert not fail_attempt(db_engine, jid, old, retryable=True, code="transient_failure")
    assert state(client, aid, job) == terminal and counts(db_engine) == (1, 1)
    finish_worker(start_worker(db_engine, queue))  # Old transport message is harmless too.
    assert counts(db_engine) == (1, 1)


def test_killed_worker_rolls_back_and_recovers_after_lease(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    job = submit(client, aid)
    process = None
    try:
        with db_engine.begin() as connection:
            connection.execute(text("SELECT id FROM accounts WHERE id=:aid FOR UPDATE"), {"aid": aid})
            process = start_worker(db_engine, queue)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with db_engine.connect() as observer:
                    waiting = observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name=:name AND wait_event_type='Lock')"), {"name": queue.name})
                if waiting:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Worker never reached the locked reconciliation transaction")
            # Even an expired lease cannot steal the job lock from active domain work.
            with db_engine.connect() as observer:
                future = observer.scalar(select(func.clock_timestamp())) + timedelta(seconds=400)
            assert recover_expired(db_engine, now=future) == 0
            process.kill()
            process.communicate(timeout=10)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert state(client, aid, job)["status"] == "running" and counts(db_engine) == (0, 0)
    with db_engine.begin() as connection:
        connection.execute(update(Job).where(Job.id == UUID(job["id"])).values(
            lease_expires_at=func.clock_timestamp() - text("interval '1 second'")))
    assert recover_expired(db_engine) == 1
    due(db_engine, job["id"])
    dispatch_due(db_engine, queue)
    finish_worker(start_worker(db_engine, queue))
    final = state(client, aid, job)
    assert final["status"] == "succeeded" and final["attempt_count"] == 2
    assert counts(db_engine) == (1, 1)


def test_permanent_dispatch_failure_stops_immediately(client, db_engine, queue, monkeypatch):
    from redis.exceptions import AuthenticationError

    aid = account(client)
    with monkeypatch.context() as patch:
        def denied(*args, **kwargs):
            raise AuthenticationError("private credentials")
        patch.setattr(queue, "enqueue", denied)
        job = submit(client, aid)
    assert job["status"] == "failed" and job["attempt_count"] == 0
    assert job["failure_details"]["retryable"] is False
    assert job["next_attempt_at"] is None and job["dispatch_after"] is None
    assert dispatch_due(db_engine, queue) == 0
    assert "credentials" not in str(job)


def test_completion_write_failure_rolls_back_decisions_and_audit(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    with db_engine.begin() as connection:
        connection.execute(text("CREATE SEQUENCE completion_failures"))
        connection.execute(text("""
            CREATE FUNCTION fail_completion_once() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.status = 'succeeded' AND nextval('completion_failures') = 1 THEN
                    RAISE EXCEPTION 'private completion failure' USING ERRCODE = '40001';
                END IF;
                RETURN NEW;
            END $$;
        """))
        connection.execute(text("CREATE TRIGGER fail_completion BEFORE UPDATE ON reconciliation_jobs "
                                "FOR EACH ROW EXECUTE FUNCTION fail_completion_once()"))
    job = submit(client, aid)
    finish_worker(start_worker(db_engine, queue))
    assert state(client, aid, job)["status"] == "queued"
    assert counts(db_engine) == (0, 0)  # Domain writes preceded the failing completion write.
    due(db_engine, job["id"])
    dispatch_due(db_engine, queue)
    finish_worker(start_worker(db_engine, queue))
    assert state(client, aid, job)["status"] == "succeeded" and counts(db_engine) == (1, 1)


def test_migrate_existing_jobs_into_recovery_state(client, db_engine):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData, Table, null
    from uuid import uuid4
    from test_jobs import ROOT

    aid = UUID(account(client))
    config = Config(str(ROOT / "alembic.ini"))
    identifiers = {}
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0005_reconciliation_jobs")
        legacy = Table("reconciliation_jobs", MetaData(), autoload_with=connection)
        current = connection.scalar(select(func.clock_timestamp()))
        for label, status, attempts in (("queued", "queued", 0), ("lost", "failed", 0),
                                        ("running", "running", 1), ("done", "succeeded", 1),
                                        ("permanent", "failed", 1)):
            jid = uuid4()
            identifiers[label] = jid
            connection.execute(legacy.insert().values(id=jid, account_id=aid, request_key=label,
                request_payload={"account_id": str(aid)}, status=status, attempt_count=attempts,
                started_at=current if attempts else None,
                finished_at=current if status in ("failed", "succeeded") else None,
                result_summary={} if status == "succeeded" else null(),
                failure_details={"code": "dispatch_failed" if label == "lost" else "reconciliation_failed"}
                                if status == "failed" else null()))
        command.upgrade(config, "head")
    assert get_job(db_engine, identifiers["lost"], aid)["status"] == "queued"
    assert get_job(db_engine, identifiers["queued"], aid)["dispatch_after"] is not None
    assert get_job(db_engine, identifiers["done"], aid)["status"] == "succeeded"
    assert get_job(db_engine, identifiers["permanent"], aid)["status"] == "failed"
    assert recover_expired(db_engine) == 1
    recovered = get_job(db_engine, identifiers["running"], aid)
    assert recovered["status"] == "queued" and recovered["attempt_count"] == 1
