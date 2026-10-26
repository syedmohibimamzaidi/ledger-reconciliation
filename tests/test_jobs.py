"""PostgreSQL + Redis integration, including execution in a separate process."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from sqlalchemy import func, select, text

from ledger.api.app import create_app
from ledger.jobs.service import execute_job, get_job
from ledger.persistence.models import AuditEvent, ReconciliationDecision, ReconciliationJob

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def queue():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("set TEST_REDIS_URL to run Redis integration tests")
    connection = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=5)
    connection.ping()  # Configured connection failures fail; no fake Redis.
    queue = Queue("test-ledger-" + uuid4().hex, connection=connection, serializer=JSONSerializer)
    try:
        yield queue
    finally:
        queue.delete(delete_jobs=True)
        connection.close()


@pytest.fixture
def client(db_engine, queue, monkeypatch):
    monkeypatch.setenv("LEDGER_DEMO_TOKEN", "test-only-jobs-token")
    monkeypatch.setenv("LEDGER_DEMO_IDENTITY", "demo-job-reviewer")
    with TestClient(create_app(engine=db_engine, job_queue=queue),
                    headers={"Authorization": "Bearer test-only-jobs-token"}) as client:
        yield client


def post(client, path, body=None):
    response = client.post(path, json=body)
    assert response.is_success, response.text
    return response


def account(client):
    return post(client, "/accounts", {"name": "Jobs integration"}).json()["id"]


def seed(client, aid, merchant="Shop", ambiguous=False):
    rid = post(client, "/receipts", {"merchant": merchant, "receipt_date": "2026-10-22", "amount": "12.30",
        "currency": "CAD", "source": "jobs-test", "source_receipt_id": str(uuid4())}).json()["receipt_id"]
    post(client, f"/accounts/{aid}/receipts/assign", {"receipt_ids": [rid]})
    csv = f"source_transaction_id,date,merchant,amount,currency\na,2026-10-22,{merchant},-12.30,CAD\n"
    if ambiguous:
        csv += f"b,2026-10-22,{merchant},-12.30,CAD\n"
    post(client, f"/accounts/{aid}/transactions/import", {"csv_text": csv})
    return rid


def start_worker(db_engine, queue):
    with db_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    env = dict(os.environ, DATABASE_URL=db_engine.url.render_as_string(hide_password=False),
               REDIS_URL=os.environ["TEST_REDIS_URL"], LEDGER_QUEUE_NAME=queue.name,
               PGOPTIONS=f"-csearch_path={schema}", PYTHONPATH=str(ROOT / "src"))
    return subprocess.Popen([sys.executable, "-m", "ledger.jobs.worker", "--burst"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def finish_worker(process):
    try:
        output, _ = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        pytest.fail("Worker did not finish: " + output)
    assert process.returncode == 0, output
    return output


def submit(client, aid, key=None):
    response = post(client, f"/accounts/{aid}/reconciliation-jobs", {"request_key": key or str(uuid4())})
    assert response.status_code == 202
    assert response.headers["location"].endswith(response.json()["id"])
    return response.json()


def test_submit_run_poll_and_completed_delivery_protection(client, db_engine, queue):
    aid, other = account(client), account(client)
    rid = seed(client, aid)
    seed(client, other)
    job = submit(client, aid, "stable-request")
    path = f"/accounts/{aid}/reconciliation-jobs/{job['id']}"
    assert job["status"] == "queued" and job["attempt_count"] == 0
    assert job["started_at"] is None and job["result_summary"] is None
    assert client.get(path).json() == job
    assert submit(client, aid, "stable-request")["id"] == job["id"]
    assert queue.count == 1
    finish_worker(start_worker(db_engine, queue))
    done = client.get(path).json()
    assert done["status"] == "succeeded" and done["attempt_count"] == 1
    assert done["result_summary"] == {"matched": 1, "needs_review": 0, "unmatched": 0,
                                       "unmatched_transactions": 0, "decisions_written": 1}
    assert done["created_at"] <= done["started_at"] <= done["finished_at"]
    assert done["failure_details"] is None
    assert submit(client, aid, "stable-request") == done
    assert queue.count == 0
    assert client.get(f"/accounts/{other}/reconciliation-jobs/{job['id']}").status_code == 404
    assert client.get(f"/accounts/{other}/outcomes").json()["items"] == []
    before = client.get(f"/accounts/{aid}/audit-events").json()
    queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
    finish_worker(start_worker(db_engine, queue))
    assert client.get(path).json() == done
    assert client.get(f"/accounts/{aid}/audit-events").json() == before
    assert client.get(f"/accounts/{aid}/outcomes").json()["items"][0]["receipt_id"] == rid


def test_running_transition_and_duplicate_claim(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    job = submit(client, aid)
    process = None
    try:
        # The worker can claim a job, but reconciliation must wait for its account lock.
        with db_engine.begin() as connection:
            connection.execute(text("SELECT id FROM accounts WHERE id=:aid FOR UPDATE"), {"aid": aid})
            process = start_worker(db_engine, queue)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = client.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}").json()
                if state["status"] == "running":
                    break
                time.sleep(.05)
            else:
                pytest.fail("Worker never claimed the job")
            assert state["attempt_count"] == 1 and state["finished_at"] is None
            execute_job(db_engine, UUID(job["id"]))  # A duplicate cannot claim a running job.
            assert get_job(db_engine, UUID(job["id"]), UUID(aid))["status"] == "running"
    finally:
        if process is not None:
            finish_worker(process)
    assert client.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}").json()["status"] == "succeeded"


def test_worker_failure_is_safe_atomic_and_not_retried(client, db_engine, queue):
    aid = account(client)
    seed(client, aid)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT secret_failure_details CHECK (event_type <> 'receipt.reconciliation_decided')"))
    job = submit(client, aid, "failure-request")
    finish_worker(start_worker(db_engine, queue))
    path = f"/accounts/{aid}/reconciliation-jobs/{job['id']}"
    failed = client.get(path).json()
    assert failed["status"] == "failed" and failed["attempt_count"] == 1
    assert failed["failure_details"]["code"] == "reconciliation_failed"
    assert failed["finished_at"] is not None and failed["result_summary"] is None
    assert "secret_failure_details" not in str(failed) and "INSERT" not in str(failed)
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ReconciliationDecision)) == 0
        assert connection.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "receipt.reconciliation_decided")) == 0
    assert submit(client, aid, "failure-request") == failed
    queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
    finish_worker(start_worker(db_engine, queue))
    assert client.get(path).json() == failed


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_background_run_preserves_human_decisions(client, db_engine, queue, action):
    aid = account(client)
    rid = seed(client, aid, ambiguous=True)
    post(client, f"/accounts/{aid}/reconciliation")
    case = client.get(f"/accounts/{aid}/review-cases").json()["items"][0]
    post(client, f"/accounts/{aid}/review-cases/{rid}/{action}", {
        "request_id": str(uuid4()), "expected_revision": case["revision"], "reason": "Verified",
        "bank_transaction_id": case["candidates"][0]["bank_transaction_id"]})
    before = client.get(f"/accounts/{aid}/outcomes").json()
    audit = client.get(f"/accounts/{aid}/audit-events").json()
    job = submit(client, aid)
    finish_worker(start_worker(db_engine, queue))
    result = client.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}").json()
    assert result["status"] == "succeeded" and result["result_summary"]["decisions_written"] == 0
    assert client.get(f"/accounts/{aid}/outcomes").json() == before
    assert client.get(f"/accounts/{aid}/audit-events").json() == audit


def test_identity_conflict_and_validation(client, db_engine, queue):
    aid, other = account(client), account(client)
    submit(client, aid, "identity-key")
    conflict = client.post(f"/accounts/{other}/reconciliation-jobs", json={"request_key": "identity-key"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "identity_conflict"
    assert queue.count == 1
    for body in ({}, {"request_key": " "}, {"request_key": "x" * 256}, {"request_key": "x", "retry": True}):
        assert client.post(f"/accounts/{aid}/reconciliation-jobs", json=body).status_code == 422
    assert client.post(f"/accounts/{uuid4()}/reconciliation-jobs", json={"request_key": "x"}).status_code == 404
    assert client.get(f"/accounts/{aid}/reconciliation-jobs/{uuid4()}").status_code == 404
    assert client.get(f"/accounts/{aid}/reconciliation-jobs/invalid").status_code == 422
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ReconciliationJob)) == 1


def test_concurrent_identical_submission_dispatches_once(client, queue):
    aid = account(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: submit(client, aid, "concurrent-key"), range(2)))
    assert jobs[0]["id"] == jobs[1]["id"] and queue.count == 1


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic abc"])
def test_job_routes_require_auth(client, authorization):
    aid = account(client)
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(client.app) as anonymous:
        for method, path, body in (("POST", f"/accounts/{aid}/reconciliation-jobs", {"request_key": "x"}),
                                   ("GET", f"/accounts/{aid}/reconciliation-jobs/{uuid4()}", None)):
            response = anonymous.request(method, path, json=body, headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"


def test_dispatch_failure_is_persisted_and_key_not_redispatched(client, db_engine, monkeypatch):
    aid = account(client)
    unavailable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=.1, socket_timeout=.1)
    queue = Queue("unavailable", connection=unavailable, serializer=JSONSerializer)
    try:
        with TestClient(create_app(engine=db_engine, job_queue=queue), headers=client.headers) as failing:
            job = submit(failing, aid, "dispatch-failure")
            assert job["status"] == "failed" and job["attempt_count"] == 0
            assert job["failure_details"]["code"] == "dispatch_failed"
            assert job["started_at"] is None and job["finished_at"] is not None
            assert submit(failing, aid, "dispatch-failure") == job
    finally:
        unavailable.close()


def test_sync_and_status_work_without_redis_configuration(client, db_engine, monkeypatch):
    aid = account(client)
    job = submit(client, aid)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with TestClient(create_app(engine=db_engine), headers=client.headers) as no_queue:
        assert no_queue.post(f"/accounts/{aid}/reconciliation").status_code == 200
        assert no_queue.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}").status_code == 200
        assert submit(no_queue, aid, job["request_key"])["id"] == job["id"]
        response = no_queue.post(f"/accounts/{aid}/reconciliation-jobs", json={"request_key": "new"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "jobs_unavailable"


def test_job_migration_roundtrip_constraints_and_populated_downgrade(client, db_engine):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    config = Config(str(ROOT / "alembic.ini"))
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_human_review")
        command.upgrade(config, "head")
    aid = account(client)
    job = submit(client, aid)
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(update(ReconciliationJob).values(status="succeeded"))
    assert get_job(db_engine, UUID(job["id"]), UUID(aid))["status"] == "queued"
    with pytest.raises(RuntimeError, match="Cannot downgrade populated reconciliation jobs"):
        with db_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0004_human_review")
    assert client.get(f"/accounts/{aid}/reconciliation-jobs/{job['id']}").status_code == 200
