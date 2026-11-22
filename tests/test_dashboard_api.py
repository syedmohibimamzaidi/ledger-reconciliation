"""Dashboard reads use real PostgreSQL snapshots and reconciliation transactions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from ledger.jobs.service import claim_attempt, execute_job, fail_attempt, submit_job
from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, AuditEvent, BankTransaction, ReconciliationDecision
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account, reconcile_account_in_transaction
from test_api import client, post
from test_jobs import queue


def account(client):
    return post(client, "/accounts", {"name": "Dashboard"})["id"]


def receipt(engine, aid=None, merchant="Coffee"):
    rid = ingest_receipt(engine, StructuredReceipt(merchant, date(2026, 10, 22), Decimal("12.30"),
        "CAD", "dashboard-test", str(uuid4()))).receipt_id
    if aid:
        assign_receipts_to_account(engine, UUID(aid), [rid])
    return rid


def bank(engine, aid, rows):
    return import_bank_transactions(engine, UUID(aid),
        "source_transaction_id,date,merchant,amount,currency\n" + rows)


def summary(client, aid):
    response = client.get(f"/accounts/{aid}/summary")
    assert response.status_code == 200, response.text
    return response.json()


def test_summary_partitions_debits_credits_zero_and_receipt_states(client, db_engine):
    aid, other = account(client), account(client)
    assert summary(client, aid) == {"account_id": aid, "last_reconciled_at": None,
        "receipts": {"total": 0, "matched": 0, "needs_review": 0, "unmatched": 0,
                     "without_decision": 0, "assigned_since_last_run": 0},
        "transactions": {"total": 0, "matched_debits": 0, "unmatched_debits": 0,
                         "credits_not_eligible": 0, "zero_amount_not_eligible": 0, "imported_since_last_run": 0},
        "active_jobs": {"queued": 0, "retrying": 0, "running": 0}, "latest_job": None,
        "has_new_reconciliation_inputs": False}
    receipt(db_engine, aid)
    receipt(db_engine)  # Unassigned records are not part of the account summary.
    receipt(db_engine, other)
    bank(db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\nb,2026-10-22,Other,-1.00,CAD\nc,2026-10-22,Income,100.00,CAD\nd,2026-10-22,Zero,0.00,CAD\n")
    before = summary(client, aid)
    assert before["receipts"]["without_decision"] == 1
    assert before["transactions"]["unmatched_debits"] == 2
    assert before["has_new_reconciliation_inputs"] is True
    old_result = post(client, f"/accounts/{aid}/reconciliation")
    assert old_result["unmatched_transactions"] == 3  # Preserve the old sync contract.
    after = summary(client, aid)
    assert after["receipts"] == {"total": 1, "matched": 1, "needs_review": 0, "unmatched": 0,
                                 "without_decision": 0, "assigned_since_last_run": 0}
    assert after["transactions"] == {"total": 4, "matched_debits": 1, "unmatched_debits": 1,
        "credits_not_eligible": 1, "zero_amount_not_eligible": 1, "imported_since_last_run": 0}
    assert after["last_reconciled_at"] is not None and not after["has_new_reconciliation_inputs"]


def test_new_inputs_are_visible_even_with_existing_decisions_and_old_receipts(client, db_engine):
    aid = account(client)
    rid = receipt(db_engine, aid)
    old_unassigned = receipt(db_engine)
    post(client, f"/accounts/{aid}/reconciliation")
    first = summary(client, aid)
    assert first["receipts"]["unmatched"] == 1 and first["receipts"]["without_decision"] == 0
    bank(db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\n")
    pending = summary(client, aid)
    assert pending["receipts"]["without_decision"] == 0
    assert pending["transactions"]["imported_since_last_run"] == 1
    assert pending["has_new_reconciliation_inputs"] is True
    # An older receipt assigned after the run must be counted by scope-event time.
    assign_receipts_to_account(db_engine, UUID(aid), [old_unassigned])
    assert summary(client, aid)["receipts"]["assigned_since_last_run"] == 1
    post(client, f"/accounts/{aid}/reconciliation")
    with db_engine.connect() as connection:
        prior = connection.execute(select(ReconciliationDecision.__table__).where(
            ReconciliationDecision.receipt_id == rid)).one()
    cutoff = summary(client, aid)["last_reconciled_at"]
    assert post(client, f"/accounts/{aid}/reconciliation")["decisions_written"] == 0
    assert summary(client, aid)["last_reconciled_at"] > cutoff
    assert not summary(client, aid)["has_new_reconciliation_inputs"]
    with db_engine.connect() as connection:
        assert connection.execute(select(ReconciliationDecision.__table__).where(
            ReconciliationDecision.receipt_id == rid)).one() == prior


def test_summary_is_one_nonlocking_snapshot_across_uncommitted_reconciliation(client, db_engine):
    aid = account(client)
    receipt(db_engine, aid)
    post(client, f"/accounts/{aid}/reconciliation")
    bank(db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\n")
    old = summary(client, aid)
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db_engine.begin() as connection:
            reconcile_account_in_transaction(connection, UUID(aid))
            event.listen(db_engine, "before_cursor_execute", capture)
            try:
                # A locking summary would block on the account above and time out.
                during = pool.submit(summary, client, aid).result(timeout=3)
            finally:
                event.remove(db_engine, "before_cursor_execute", capture)
            assert during == old
    assert len(statements) == 1 and "FOR UPDATE" not in statements[0]
    assert "FILTER" in statements[0]
    after = summary(client, aid)
    assert after["receipts"]["matched"] == 1 and after["receipts"]["unmatched"] == 0
    assert after["transactions"]["matched_debits"] == 1 and after["transactions"]["unmatched_debits"] == 0
    assert not after["has_new_reconciliation_inputs"]


def test_import_started_before_cutoff_but_waiting_for_run_remains_new(client, db_engine):
    aid = account(client)
    started = Event()
    def capture(connection, cursor, statement, parameters, context, executemany):
        if "FOR KEY SHARE" in statement:
            started.set()
    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db_engine.begin() as connection:
                connection.execute(select(Account.id).where(Account.id == UUID(aid)).with_for_update())
                future = pool.submit(bank, db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\n")
                assert started.wait(timeout=3)
                reconcile_account_in_transaction(connection, UUID(aid))
            assert future.result(timeout=3).inserted == 1
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    pending = summary(client, aid)
    assert pending["transactions"]["imported_since_last_run"] == 1 and pending["has_new_reconciliation_inputs"]
    bank(db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\n")  # Duplicate imports don't add inputs.
    assert summary(client, aid)["transactions"]["imported_since_last_run"] == 1


def test_cutoff_and_domain_writes_roll_back_together(client, db_engine):
    aid = account(client)
    receipt(db_engine, aid)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE accounts ADD CONSTRAINT no_cutoff CHECK (last_reconciled_at IS NULL)"))
    with pytest.raises(IntegrityError):
        reconcile_account(db_engine, UUID(aid))
    assert summary(client, aid)["last_reconciled_at"] is None
    assert summary(client, aid)["receipts"]["without_decision"] == 1
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "receipt.reconciliation_decided")) == 0


def test_details_filters_assignment_and_audit_order(client, db_engine):
    aid, other = account(client), account(client)
    rid = receipt(db_engine)
    detail = client.get(f"/receipts/{rid}").json()
    assert detail["decision"] is None and detail["account_id"] is None and detail["amount"] == "12.30"
    assert client.get("/receipts?unassigned=true").json()["items"][0]["id"] == str(rid)
    assert client.get(f"/receipts?unassigned=true&account_id={aid}").status_code == 422
    assign_receipts_to_account(db_engine, UUID(aid), [rid])
    assert client.get("/receipts?unassigned=true").json()["items"] == []
    bank(db_engine, aid, "a,2026-10-22,Coffee,-12.30,CAD\n")
    tid = client.get(f"/accounts/{aid}/transactions").json()["items"][0]["id"]
    assert client.get(f"/accounts/{aid}/transactions/{tid}").json()["amount"] == "-12.30"
    assert client.get(f"/accounts/{other}/transactions/{tid}").status_code == 404
    post(client, f"/accounts/{aid}/reconciliation")
    assert client.get(f"/receipts/{rid}").json()["decision"]["status"] == "matched"
    assert len(client.get(f"/accounts/{aid}/outcomes?status=matched&receipt_id={rid}").json()["items"]) == 1
    assert client.get(f"/accounts/{aid}/outcomes?status=unmatched").json()["items"] == []
    assert client.get(f"/accounts/{other}/outcomes?receipt_id={rid}").json()["items"] == []
    oldest = client.get(f"/accounts/{aid}/audit-events").json()["items"]
    newest = client.get(f"/accounts/{aid}/audit-events?order=newest").json()["items"]
    assert newest == list(reversed(oldest))
    for route in (f"/accounts/{uuid4()}/summary", f"/receipts/{uuid4()}", f"/accounts/{aid}/transactions/{uuid4()}"):
        assert client.get(route).status_code == 404
    assert client.get(f"/accounts/{aid}/outcomes?status=invalid").status_code == 422
    assert client.get(f"/accounts/{aid}/audit-events?order=invalid").status_code == 422


def test_latest_job_replaces_historical_failure_count_and_lists_retrying(client, db_engine, queue):
    aid, other = account(client), account(client)
    failed = submit_job(db_engine, queue, UUID(aid), str(uuid4()))
    token = claim_attempt(db_engine, failed["id"])
    fail_attempt(db_engine, failed["id"], token, retryable=False, code="reconciliation_failed")
    succeeded = submit_job(db_engine, queue, UUID(aid), str(uuid4()))
    execute_job(db_engine, succeeded["id"])
    state = summary(client, aid)
    assert state["latest_job"]["status"] == "succeeded" and "failed" not in state["active_jobs"]
    assert state["last_reconciled_at"] is not None  # Background and sync share the cutoff.
    retry = submit_job(db_engine, queue, UUID(aid), str(uuid4()))
    token = claim_attempt(db_engine, retry["id"])
    fail_attempt(db_engine, retry["id"], token, retryable=True, code="transient_failure")
    submit_job(db_engine, queue, UUID(other), str(uuid4()))
    state = summary(client, aid)
    assert state["latest_job"]["display_status"] == "retrying"
    assert state["active_jobs"] == {"queued": 0, "retrying": 1, "running": 0}
    listing = client.get(f"/accounts/{aid}/reconciliation-jobs").json()
    assert [job["id"] for job in listing["items"]] == [str(row["id"]) for row in (retry, succeeded, failed)]
    assert client.get(f"/accounts/{aid}/reconciliation-jobs?status=retrying").json()["items"][0]["id"] == str(retry["id"])
    assert client.get(f"/accounts/{aid}/reconciliation-jobs?status=queued").json()["items"] == []
    assert len(client.get(f"/accounts/{aid}/reconciliation-jobs?limit=1&offset=1").json()["items"]) == 1
    assert client.get(f"/accounts/{aid}/reconciliation-jobs?limit=101").status_code == 422
    assert client.get(f"/accounts/{aid}/reconciliation-jobs?status=unknown").status_code == 422
    assert "attempt_token" not in str(listing) and "request_payload" not in str(listing)


def test_decisions_never_sort_before_inputs_committed_while_run_was_pending(client, db_engine):
    """A run whose transaction began before an import must still record its decisions after it."""
    aid = account(client)
    rid = receipt(db_engine, aid)
    with db_engine.connect() as connection, connection.begin():
        connection.execute(text("SELECT 1"))  # Fixes this transaction's now() before the import.
        bank(db_engine, aid, "late,2026-10-22,Coffee,-12.30,CAD\n")
        assert reconcile_account_in_transaction(connection, UUID(aid)).matched == 1
    with db_engine.connect() as connection:
        imported = connection.scalar(select(AuditEvent.created_at).where(
            AuditEvent.event_type == "bank_transaction.created",
            AuditEvent.bank_transaction_id.in_(select(BankTransaction.id).where(BankTransaction.account_id == UUID(aid)))))
        decided = connection.scalar(select(AuditEvent.created_at).where(
            AuditEvent.event_type == "receipt.reconciliation_decided", AuditEvent.receipt_id == rid))
        decision = connection.execute(select(ReconciliationDecision.created_at, ReconciliationDecision.updated_at)
                                      .where(ReconciliationDecision.receipt_id == rid)).one()
        cutoff = connection.scalar(select(Account.last_reconciled_at).where(Account.id == UUID(aid)))
    assert imported < decided == decision.created_at == decision.updated_at == cutoff
    events = client.get(f"/accounts/{aid}/audit-events", params={"order": "oldest"}).json()["items"]
    types = [item["event_type"] for item in events]
    assert types.index("bank_transaction.created") < types.index("receipt.reconciliation_decided")
