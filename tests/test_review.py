from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from threading import Barrier, local
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, AuditEvent, BankTransaction, Receipt, ReconciliationDecision, ReviewAction
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account
from ledger.persistence.review import (
    ReviewConflict, StaleReview, confirm_candidate, list_review_cases, reject_candidate,
)


def add_account(engine):
    with engine.begin() as connection:
        return connection.execute(insert(Account).values(name="Review test").returning(Account.id)).scalar_one()


def add_receipt(engine, aid):
    rid = ingest_receipt(engine, StructuredReceipt(
        "Blue Bottle", date(2026, 10, 22), Decimal("12.30"), "CAD", "review-test", str(uuid4()))).receipt_id
    assign_receipts_to_account(engine, aid, [rid])
    return rid


def add_transaction(engine, aid, amount="-12.30"):
    source_id = str(uuid4())
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["source_transaction_id", "date", "merchant", "amount", "currency"])
    writer.writerow([source_id, "2026-10-22", "Blue Bottle", amount, "CAD"])
    import_bank_transactions(engine, aid, buffer.getvalue())
    with engine.connect() as connection:
        return connection.scalar(select(BankTransaction.id).where(BankTransaction.source_transaction_id == source_id))


@pytest.fixture
def case(db_engine):
    aid = add_account(db_engine)
    rid = add_receipt(db_engine, aid)
    tids = [add_transaction(db_engine, aid), add_transaction(db_engine, aid)]
    reconcile_account(db_engine, aid)
    return aid, rid, tids


def state(engine, rid):
    with engine.connect() as connection:
        return connection.execute(select(ReconciliationDecision.__table__).where(
            ReconciliationDecision.receipt_id == rid)).mappings().one()


def counts(engine):
    with engine.connect() as connection:
        return (connection.scalar(select(func.count()).select_from(ReviewAction)),
                connection.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "receipt.reviewed")))


def request(**changes):
    return {"expected_revision": 1, "request_id": uuid4(), "reviewer": "reviewer@example.test",
            "reason": "Checked the statement and receipt", **changes}


def test_list_confirm_and_rerun_preserve_human_decision(db_engine, case):
    aid, rid, tids = case
    listed = list_review_cases(db_engine, aid)
    assert len(listed) == 1 and listed[0].receipt_id == rid and listed[0].revision == 1
    assert listed[0].reasons == ("multiple_transactions_for_receipt",)
    assert len(listed[0].candidates) == 2 and listed[0].rejected_candidates == ()
    result = confirm_candidate(db_engine, aid, rid, tids[0], **request())
    assert result.status == "matched" and result.revision == 2 and not result.repeated
    assert result.reviewed_at.tzinfo is not None
    before = state(db_engine, rid)
    assert before["bank_transaction_id"] == tids[0]
    assert before["details"]["reasons"] == ["human_confirmed"]
    assert list_review_cases(db_engine, aid) == []
    assert reconcile_account(db_engine, aid).decisions_written == 0
    assert state(db_engine, rid) == before and counts(db_engine) == (1, 1)
    with db_engine.connect() as connection:
        audit = connection.execute(select(AuditEvent.__table__).where(AuditEvent.event_type == "receipt.reviewed")).one()
        action = connection.execute(select(ReviewAction.__table__)).one()
        assert audit.review_action_id == result.action_id == action.id
        assert audit.payload["reviewer"] == action.reviewer == "reviewer@example.test"
        assert audit.payload["action"] == "confirm" and audit.payload["reason"] == action.reason
        assert audit.created_at == action.created_at == result.reviewed_at


def test_amount_mismatch_requires_acknowledgement_and_reason_preserves_amounts(db_engine):
    aid = add_account(db_engine)
    rid = add_receipt(db_engine, aid)
    tid = add_transaction(db_engine, aid, "-15.00")
    reconcile_account(db_engine, aid)
    inputs = request()
    with pytest.raises(ValueError, match="acknowledgement"):
        confirm_candidate(db_engine, aid, rid, tid, **inputs)
    with pytest.raises(ValueError, match="reason"):
        confirm_candidate(db_engine, aid, rid, tid, **{**inputs, "reason": " "}, acknowledge_amount_mismatch=True)
    assert counts(db_engine) == (0, 0) and state(db_engine, rid)["revision"] == 1
    result = confirm_candidate(db_engine, aid, rid, tid, **inputs, acknowledge_amount_mismatch=True)
    assert result.status == "matched"
    with db_engine.connect() as connection:
        assert connection.scalar(select(Receipt.amount).where(Receipt.id == rid)) == Decimal("12.30")
        assert connection.scalar(select(BankTransaction.amount).where(BankTransaction.id == tid)) == Decimal("-15.00")
        action = connection.execute(select(ReviewAction.__table__)).one()
        audit = connection.execute(select(AuditEvent.__table__).where(AuditEvent.review_action_id == result.action_id)).one()
        assert (action.receipt_amount, action.bank_amount) == (Decimal("12.30"), Decimal("-15.00"))
        assert audit.payload["receipt"]["amount"] == "12.30"
        assert audit.payload["transaction"]["amount"] == "-15.00"
        assert audit.payload["acknowledge_amount_mismatch"] is True
    assert reconcile_account(db_engine, aid).decisions_written == 0


def test_reject_one_candidate_retains_other_and_survives_rerun(db_engine, case):
    aid, rid, tids = case
    result = reject_candidate(db_engine, aid, rid, tids[0], **request(reason="Duplicate authorization"))
    assert result.status == "needs_review" and result.revision == 2
    remaining = list_review_cases(db_engine, aid)[0]
    assert [item["bank_transaction_id"] for item in remaining.candidates] == [str(tids[1])]
    assert remaining.rejected_candidates[0]["bank_transaction_id"] == str(tids[0])
    assert remaining.rejected_candidates[0]["reason"] == "Duplicate authorization"
    assert remaining.reasons == ("human_review_required",)
    assert reconcile_account(db_engine, aid).decisions_written == 0
    assert state(db_engine, rid)["bank_transaction_id"] is None
    with pytest.raises(ReviewConflict, match="rejected"):
        confirm_candidate(db_engine, aid, rid, tids[0], **request(expected_revision=2))
    accepted = confirm_candidate(db_engine, aid, rid, tids[1], **request(expected_revision=2))
    assert accepted.status == "matched" and accepted.revision == 3
    assert reconcile_account(db_engine, aid).decisions_written == 0
    assert state(db_engine, rid)["bank_transaction_id"] == tids[1] and counts(db_engine) == (2, 2)


def test_reject_last_candidate_is_unmatched_and_future_candidates_still_need_review(db_engine):
    aid = add_account(db_engine)
    rid = add_receipt(db_engine, aid)
    tid = add_transaction(db_engine, aid, "-15.00")
    reconcile_account(db_engine, aid)
    result = reject_candidate(db_engine, aid, rid, tid, **request())
    assert result.status == "unmatched" and list_review_cases(db_engine, aid) == []
    assert state(db_engine, rid)["details"]["reasons"] == ["all_candidates_rejected"]
    assert reconcile_account(db_engine, aid).decisions_written == 0
    new_tid = add_transaction(db_engine, aid)
    assert reconcile_account(db_engine, aid).needs_review == 1
    listed = list_review_cases(db_engine, aid)[0]
    assert [item["bank_transaction_id"] for item in listed.candidates] == [str(new_tid)]


@pytest.mark.parametrize("action", [confirm_candidate, reject_candidate])
def test_identical_retries_are_idempotent(db_engine, case, action):
    aid, rid, tids = case
    inputs = request()
    first = action(db_engine, aid, rid, tids[0], **inputs)
    before = state(db_engine, rid)
    second = action(db_engine, aid, rid, tids[0], **inputs)
    assert second.action_id == first.action_id and second.reviewed_at == first.reviewed_at
    assert second.revision == first.revision and second.status == first.status and second.repeated
    assert state(db_engine, rid) == before and counts(db_engine) == (1, 1)
    with pytest.raises(ReviewConflict, match="different review input"):
        action(db_engine, aid, rid, tids[0], **{**inputs, "reason": "Changed request"})


def test_retry_returns_original_result_after_later_action(db_engine, case):
    aid, rid, tids = case
    inputs = request()
    rejected = reject_candidate(db_engine, aid, rid, tids[0], **inputs)
    confirm_candidate(db_engine, aid, rid, tids[1], **request(expected_revision=2))
    replayed = reject_candidate(db_engine, aid, rid, tids[0], **inputs)
    assert replayed.action_id == rejected.action_id and replayed.status == "needs_review" and replayed.revision == 2
    assert state(db_engine, rid)["status"] == "matched" and counts(db_engine) == (2, 2)


def test_stale_revision_and_accepted_case_conflicts(db_engine, case):
    aid, rid, tids = case
    confirm_candidate(db_engine, aid, rid, tids[0], **request())
    with pytest.raises(StaleReview, match="expected 1, current 2"):
        reject_candidate(db_engine, aid, rid, tids[1], **request())
    with pytest.raises(ReviewConflict, match="accepted matches cannot be changed"):
        reject_candidate(db_engine, aid, rid, tids[1], **request(expected_revision=2))
    assert counts(db_engine) == (1, 1)


def test_reconciliation_revision_invalidates_old_review(db_engine, case):
    aid, rid, tids = case
    add_transaction(db_engine, aid)
    reconcile_account(db_engine, aid)
    with pytest.raises(StaleReview):
        confirm_candidate(db_engine, aid, rid, tids[0], **request())
    assert counts(db_engine) == (0, 0)


def test_invalid_scope_or_unlisted_candidate_is_rejected(db_engine, case):
    aid, rid, tids = case
    other_account = add_account(db_engine)
    other_tid = add_transaction(db_engine, other_account)
    for account, receipt, transaction in ((other_account, rid, other_tid), (aid, rid, other_tid), (aid, uuid4(), tids[0])):
        with pytest.raises(ValueError):
            confirm_candidate(db_engine, account, receipt, transaction, **request())
    unlisted = add_transaction(db_engine, aid)
    with pytest.raises(ValueError, match="eligible candidate"):
        confirm_candidate(db_engine, aid, rid, unlisted, **request())
    assert counts(db_engine) == (0, 0)


@pytest.mark.parametrize("changes", [{"currency": "USD"}, {"amount": Decimal("12.30")},
                                          {"merchant": "Other"}, {"transaction_date": date(2026, 11, 1)}])
def test_confirmation_rechecks_live_eligibility(db_engine, case, changes):
    aid, rid, tids = case
    with db_engine.begin() as connection:
        connection.execute(update(BankTransaction).where(BankTransaction.id == tids[0]).values(**changes))
    with pytest.raises(ValueError, match="eligible candidate"):
        confirm_candidate(db_engine, aid, rid, tids[0], **request(acknowledge_amount_mismatch=True))
    assert counts(db_engine) == (0, 0)


def test_transaction_cannot_be_confirmed_for_two_receipts(db_engine):
    aid = add_account(db_engine)
    r1, r2 = add_receipt(db_engine, aid), add_receipt(db_engine, aid)
    tid = add_transaction(db_engine, aid)
    reconcile_account(db_engine, aid)
    confirm_candidate(db_engine, aid, r1, tid, **request())
    with pytest.raises(ReviewConflict, match="another receipt"):
        confirm_candidate(db_engine, aid, r2, tid, **request())
    reconcile_account(db_engine, aid)
    assert state(db_engine, r2)["status"] == "needs_review" and counts(db_engine) == (1, 1)


def test_rejection_applies_to_pair_not_transaction_globally(db_engine):
    aid = add_account(db_engine)
    r1, r2 = add_receipt(db_engine, aid), add_receipt(db_engine, aid)
    tid = add_transaction(db_engine, aid)
    reconcile_account(db_engine, aid)
    reject_candidate(db_engine, aid, r1, tid, **request())
    result = reconcile_account(db_engine, aid)
    assert result.matched == 1 and result.unmatched == 1
    assert state(db_engine, r1)["bank_transaction_id"] is None
    assert state(db_engine, r2)["bank_transaction_id"] == tid
    assert counts(db_engine) == (1, 1)


@pytest.mark.parametrize("action", [confirm_candidate, reject_candidate])
def test_audit_failure_rolls_back_action_case_and_rejection(db_engine, case, action):
    aid, rid, tids = case
    before, inputs = state(db_engine, rid), request()
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_review_failure CHECK (event_type <> 'receipt.reviewed')"))
    with pytest.raises(IntegrityError):
        action(db_engine, aid, rid, tids[0], **inputs)
    assert state(db_engine, rid) == before and counts(db_engine) == (0, 0)
    assert reconcile_account(db_engine, aid).decisions_written == 0
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events DROP CONSTRAINT force_review_failure"))
    assert not action(db_engine, aid, rid, tids[0], **inputs).repeated


@pytest.mark.parametrize("identical_request", [False, True])
def test_concurrent_reviews(db_engine, case, identical_request):
    aid, rid, tids = case
    barrier, thread_state = Barrier(2), local()

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if "FROM accounts" in statement and "FOR UPDATE" in statement and not getattr(thread_state, "started", False):
            thread_state.started = True
            barrier.wait(timeout=15)

    inputs = request()

    def submit(index):
        try:
            return confirm_candidate(db_engine, aid, rid, tids[0] if identical_request else tids[index],
                                     **(inputs if identical_request else request()))
        except StaleReview:
            return None

    event.listen(db_engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, [0, 1]))
    finally:
        event.remove(db_engine, "before_cursor_execute", synchronize)
    successful = [item for item in results if item is not None]
    assert len(successful) == (2 if identical_request else 1)
    assert sum(not item.repeated for item in successful) == 1
    assert counts(db_engine) == (1, 1) and state(db_engine, rid)["revision"] == 2


def test_database_review_constraints(db_engine, case):
    aid, rid, tids = case
    result = confirm_candidate(db_engine, aid, rid, tids[0], **request())
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(insert(AuditEvent).values(receipt_id=rid, review_action_id=result.action_id,
                                                        event_type="receipt.reviewed", payload={}))
    mismatch_rid = add_receipt(db_engine, aid)
    with pytest.raises(IntegrityError) as caught:
        with db_engine.begin() as connection:
            connection.execute(insert(ReviewAction).values(
                account_id=aid, receipt_id=mismatch_rid, bank_transaction_id=tids[1], request_id=uuid4(),
                action="confirm", reviewer="reviewer", reason="Missing acknowledgement", expected_revision=1,
                resulting_revision=2, resulting_status="matched", receipt_amount=Decimal("12.30"),
                bank_amount=Decimal("-15.00"), acknowledge_amount_mismatch=False, snapshot={}))
    assert caught.value.orig.diag.constraint_name == "ck_review_mismatch_acknowledged"


def test_populated_review_downgrade_is_refused(db_engine, case):
    from alembic import command
    from alembic.config import Config

    aid, rid, tids = case
    confirm_candidate(db_engine, aid, rid, tids[0], **request())
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with pytest.raises(RuntimeError, match="Cannot downgrade populated human review"):
        with db_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0003_reconciliation")
    assert state(db_engine, rid)["status"] == "matched" and counts(db_engine) == (1, 1)
