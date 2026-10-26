"""Controlled retry demo in a disposable schema/queue, with real API/worker processes.

Requires DATABASE_URL with create/drop schema privileges and REDIS_URL.
Injects one SQL serialization failure inside the private demo schema, never via API.
"""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from alembic import command
from alembic.config import Config
import httpx
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def main():
    schema = "recovery_demo_" + uuid4().hex
    queue_name = "recovery-demo-" + uuid4().hex
    engine = create_engine(os.environ["DATABASE_URL"])
    redis = Redis.from_url(os.environ["REDIS_URL"])
    queue = Queue(queue_name, connection=redis, serializer=JSONSerializer)
    processes, logs = [], []
    created = False
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, PGOPTIONS=f"-csearch_path={schema}", LEDGER_QUEUE_NAME=queue_name,
               LEDGER_DEMO_TOKEN=secrets.token_urlsafe(32), LEDGER_DEMO_IDENTITY="private-retry-demo",
               PYTHONPATH=str(ROOT / "src"))

    def start(arguments):
        log = tempfile.TemporaryFile(mode="w+")
        logs.append(log)
        process = subprocess.Popen([sys.executable, *arguments], cwd=ROOT, env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        redis.ping()
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        scoped = create_engine(os.environ["DATABASE_URL"], connect_args={"options": f"-csearch_path={schema}"})
        try:
            config = Config(str(ROOT / "alembic.ini"))
            with scoped.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.execute(text("CREATE SEQUENCE demo_failure_sequence"))
                connection.execute(text("""
                    CREATE FUNCTION demo_transient_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.event_type = 'receipt.reconciliation_decided'
                           AND nextval('demo_failure_sequence') = 1 THEN
                            RAISE EXCEPTION 'demo serialization failure' USING ERRCODE = '40001';
                        END IF;
                        RETURN NEW;
                    END $$;
                """))
                connection.execute(text("CREATE TRIGGER demo_fail_once BEFORE INSERT ON audit_events "
                                        "FOR EACH ROW EXECUTE FUNCTION demo_transient_failure()"))
        finally:
            scoped.dispose()
        start(["-m", "uvicorn", "ledger.api.app:create_app", "--factory", "--host", "127.0.0.1",
               "--port", str(port), "--no-access-log"])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10,
                          headers={"Authorization": f"Bearer {env['LEDGER_DEMO_TOKEN']}"}) as client:
            def request(method, path, body=None):
                response = client.request(method, path, json=body)
                response.raise_for_status()
                return response.json()

            deadline = time.monotonic() + 10
            while True:
                try:
                    request("GET", "/openapi.json")
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Demo API failed to start")
                    time.sleep(.1)
            aid = request("POST", "/accounts", {"name": "Private retry demo"})["id"]
            base = f"/accounts/{aid}"
            rid = request("POST", "/receipts", {"merchant": "Demo Shop", "receipt_date": "2026-10-22",
                "amount": "12.30", "currency": "CAD", "source": "retry-demo", "source_receipt_id": "one"})["receipt_id"]
            request("POST", base + "/receipts/assign", {"receipt_ids": [rid]})
            request("POST", base + "/transactions/import", {"csv_text":
                "source_transaction_id,date,merchant,amount,currency\none,2026-10-22,Demo Shop,-12.30,CAD\n"})
            job = request("POST", base + "/reconciliation-jobs", {"request_key": "private-demo-run"})
            path = base + "/reconciliation-jobs/" + job["id"]
            print("Accepted job:", job["id"], flush=True)
            # First real worker attempt fails deterministically; no dispatcher yet.
            worker = start(["-m", "ledger.jobs.worker", "--burst"])
            worker.wait(timeout=15)
            if worker.returncode:
                raise RuntimeError("First demo worker failed to run")
            failed = request("GET", path)
            assert failed["status"] == "queued" and failed["attempt_count"] == 1
            assert failed["failure_details"]["code"] == "transient_failure"
            assert request("GET", base + "/outcomes")["items"] == []
            print("Attempt 1:", failed["failure_details"], "next_attempt_at=", failed["next_attempt_at"], flush=True)
            start(["-m", "ledger.jobs.dispatcher"])
            worker = start(["-m", "ledger.jobs.worker"])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                job = request("GET", path)
                if job["status"] in ("succeeded", "failed"):
                    break
                time.sleep(.1)
            assert job["status"] == "succeeded" and job["attempt_count"] == 2
            before = request("GET", base + "/audit-events")
            assert len(request("GET", base + "/outcomes")["items"]) == 1
            assert sum(item["event_type"] == "receipt.reconciliation_decided" for item in before["items"]) == 1
            # Stop the long-lived worker, then deliberately redeliver the completed ID.
            worker.terminate()
            worker.wait(timeout=10)
            queue.enqueue("ledger.jobs.tasks.reconcile_job", job["id"], result_ttl=0)
            duplicate = start(["-m", "ledger.jobs.worker", "--burst"])
            duplicate.wait(timeout=15)
            assert duplicate.returncode == 0
            assert request("GET", path) == job
            assert request("GET", base + "/audit-events") == before
            print("Attempt 2: succeeded; duplicate delivery preserved 1 decision and 1 reconciliation audit event.", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        queue.delete(delete_jobs=True)
        redis.close()
        if created:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
