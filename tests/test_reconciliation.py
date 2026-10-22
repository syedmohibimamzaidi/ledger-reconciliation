from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from threading import Barrier, local
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.exc import IntegrityError

from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, AuditEvent, BankTransaction, Receipt, ReconciliationDecision
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account


@pytest.fixture
def account_id(db_engine):
    return add_account(db_engine)


def add_account(engine):
    with engine.begin() as connection:
        return connection.execute(insert(Account).values(name="Reconciliation test").returning(Account.id)).scalar_one()


def add_receipt(engine, account_id=None, merchant="Blue Bottle", amount="12.30", currency="CAD"):
    rid = ingest_receipt(engine, StructuredReceipt(
        merchant, date(2026, 10, 22), Decimal(amount), currency, "reconciliation-test", str(uuid4()))).receipt_id
    if account_id:
        assign_receipts_to_account(engine, account_id, [rid])
    return rid


def add_transaction(engine, account_id, merchant="Blue Bottle", amount="-12.30", currency="CAD", day="2026-10-22"):
    source_id = str(uuid4())
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["source_transaction_id", "date", "merchant", "amount", "currency"])
    writer.writerow([source_id, day, merchant, amount, currency])
    import_bank_transactions(engine, account_id, buffer.getvalue())
    with engine.connect() as connection:
        return connection.scalar(select(BankTransaction.id).where(BankTransaction.source_transaction_id == source_id))


def decisions(engine):
    with engine.connect() as connection:
        return {row.receipt_id: row for row in connection.execute(select(ReconciliationDecision.__table__))}


def event_count(engine):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "receipt.reconciliation_decided"))


def test_clear_match_persistence_and_repeat(db_engine, account_id):
    rid = add_receipt(db_engine, account_id)
    tid = add_transaction(db_engine, account_id, merchant="SQ *BLUE BOTTLE")
    first = reconcile_account(db_engine, account_id)
    before = decisions(db_engine)
    assert (first.matched, first.needs_review, first.unmatched, first.unmatched_transactions) == (1, 0, 0, 0)
    saved = before[rid]
    assert saved.bank_transaction_id == tid and saved.revision == 1
    assert saved.created_at.tzinfo is not None
    assert saved.status == "matched" and saved.details["reasons"] == ["clear_unique_exact"]
    with db_engine.connect() as connection:
        audit = connection.execute(select(AuditEvent.__table__).where(
            AuditEvent.event_type == "receipt.reconciliation_decided")).one()
        assert audit.receipt_id == rid and audit.payload["revision"] == 1
        assert audit.payload["bank_transaction_id"] == str(tid)
        assert audit.payload["candidates"][0]["amount"] == "-12.30"
    second = reconcile_account(db_engine, account_id)
    assert first.decisions_written == 1 and second.decisions_written == 0
    assert decisions(db_engine) == before and event_count(db_engine) == 1


def test_amount_mismatch_review_and_repeat(db_engine, account_id):
    rid = add_receipt(db_engine, account_id)
    add_transaction(db_engine, account_id, amount="-15.00")
    result = reconcile_account(db_engine, account_id)
    assert result.needs_review == 1 and result.matched == 0
    saved = decisions(db_engine)[rid]
    assert saved.bank_transaction_id is None
    assert saved.details["reasons"] == ["amount_difference"]
    assert saved.details["candidates"][0]["amount_difference"] == "2.70"
    assert reconcile_account(db_engine, account_id).decisions_written == 0
    assert event_count(db_engine) == 1


def test_multiple_transactions_for_one_receipt(db_engine, account_id):
    rid = add_receipt(db_engine, account_id)
    for amount in ("-12.30", "-12.31"):
        add_transaction(db_engine, account_id, amount=amount)
    assert reconcile_account(db_engine, account_id).needs_review == 1
    saved = decisions(db_engine)[rid]
    assert "multiple_transactions_for_receipt" in saved.details["reasons"]
    assert saved.bank_transaction_id is None


def test_multiple_receipts_for_one_transaction_even_with_different_amount(db_engine, account_id):
    rids = [add_receipt(db_engine, account_id, amount=amount) for amount in ("12.30", "99.00")]
    add_transaction(db_engine, account_id)
    result = reconcile_account(db_engine, account_id)
    assert result.needs_review == 2 and result.matched == 0
    for rid in rids:
        assert "multiple_receipts_for_transaction" in decisions(db_engine)[rid].details["reasons"]


@pytest.mark.parametrize("changes", [
    {"currency": "USD"}, {"amount": "12.30"}, {"amount": "0"},
    {"merchant": "Other"}, {"day": "2026-10-21"}, {"day": "2026-10-27"},
])
def test_excluded_bank_records_remain_unmatched(db_engine, account_id, changes):
    rid = add_receipt(db_engine, account_id)
    add_transaction(db_engine, account_id, **changes)
    result = reconcile_account(db_engine, account_id)
    assert result.unmatched == 1 and result.unmatched_transactions == 1
    assert decisions(db_engine)[rid].details["reasons"] == ["no_plausible_candidates"]


def test_explicit_scope_excludes_unassigned_and_other_accounts(db_engine, account_id):
    unassigned = add_receipt(db_engine)
    scoped = add_receipt(db_engine, account_id)
    other_id = add_account(db_engine)
    other_receipt = add_receipt(db_engine, other_id)
    add_transaction(db_engine, other_id)
    result = reconcile_account(db_engine, account_id)
    assert result.unmatched == 1 and result.matched == 0
    assert set(decisions(db_engine)) == {scoped}
    assert unassigned not in decisions(db_engine) and other_receipt not in decisions(db_engine)
    assert assign_receipts_to_account(db_engine, account_id, [scoped]) == 0
    with pytest.raises(ValueError, match="another account"):
        assign_receipts_to_account(db_engine, other_id, [unassigned, scoped])
    with db_engine.connect() as connection:
        assert connection.scalar(select(Receipt.account_id).where(Receipt.id == unassigned)) is None


def test_unmatched_decision_revised_when_bank_data_arrives(db_engine, account_id):
    rid = add_receipt(db_engine, account_id)
    assert reconcile_account(db_engine, account_id).unmatched == 1
    add_transaction(db_engine, account_id)
    result = reconcile_account(db_engine, account_id)
    assert result.matched == 1 and result.decisions_written == 1
    assert decisions(db_engine)[rid].revision == 2
    assert event_count(db_engine) == 2
    assert reconcile_account(db_engine, account_id).decisions_written == 0


def test_accepted_match_is_retained_and_cannot_make_later_competitors_look_unique(db_engine, account_id):
    original = add_receipt(db_engine, account_id)
    tid = add_transaction(db_engine, account_id)
    reconcile_account(db_engine, account_id)
    added = add_receipt(db_engine, account_id)
    add_transaction(db_engine, account_id)
    result = reconcile_account(db_engine, account_id)
    assert (result.matched, result.needs_review) == (1, 1)
    saved = decisions(db_engine)
    assert saved[original].bank_transaction_id == tid and saved[original].revision == 1
    assert "transaction_already_matched" in saved[added].details["reasons"]
    assert "multiple_receipts_for_transaction" in saved[added].details["reasons"]


def test_concurrent_runs_serialize_without_duplicate_outcomes_or_events(db_engine, account_id):
    add_receipt(db_engine, account_id)
    add_transaction(db_engine, account_id)
    barrier, state = Barrier(4), local()

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if "FROM accounts" in statement and "FOR UPDATE" in statement and not getattr(state, "started", False):
            state.started = True
            barrier.wait(timeout=15)

    event.listen(db_engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: reconcile_account(db_engine, account_id), range(4)))
    finally:
        event.remove(db_engine, "before_cursor_execute", synchronize)
    assert all(result.matched == 1 for result in results)
    assert sum(result.decisions_written for result in results) == 1
    assert len(decisions(db_engine)) == 1 and event_count(db_engine) == 1


def test_audit_failure_rolls_back_whole_run(db_engine, account_id):
    rids = [add_receipt(db_engine, account_id, merchant=merchant) for merchant in ("Alpha", "Beta")]
    for merchant in ("Alpha", "Beta"):
        add_transaction(db_engine, account_id, merchant=merchant)
    with db_engine.begin() as connection:
        # The later receipt's event fails after the first outcome/event succeeded.
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_reconcile_failure CHECK "
                                "(event_type <> 'receipt.reconciliation_decided' OR receipt_id <> '" +
                                str(max(rids)) + "'::uuid)"))
    with pytest.raises(IntegrityError):
        reconcile_account(db_engine, account_id)
    assert not decisions(db_engine) and event_count(db_engine) == 0
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events DROP CONSTRAINT force_reconcile_failure"))
    assert reconcile_account(db_engine, account_id).matched == 2


def test_revision_audit_failure_preserves_previous_outcome(db_engine, account_id):
    add_receipt(db_engine, account_id)
    reconcile_account(db_engine, account_id)
    before = decisions(db_engine)
    add_transaction(db_engine, account_id)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_revision_failure CHECK "
                                "(event_type <> 'receipt.reconciliation_decided' OR payload->>'revision' <> '2')"))
    with pytest.raises(IntegrityError):
        reconcile_account(db_engine, account_id)
    assert decisions(db_engine) == before and event_count(db_engine) == 1
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events DROP CONSTRAINT force_revision_failure"))
    assert reconcile_account(db_engine, account_id).matched == 1


def test_scope_assignment_rolls_back_on_audit_failure(db_engine, account_id):
    rids = [add_receipt(db_engine), add_receipt(db_engine)]
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_scope_failure CHECK "
                                "(event_type <> 'receipt.scoped' OR receipt_id <> '" + str(max(rids)) + "'::uuid)"))
    with pytest.raises(IntegrityError):
        assign_receipts_to_account(db_engine, account_id, rids)
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Receipt).where(Receipt.account_id.is_not(None))) == 0
        assert connection.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "receipt.scoped")) == 0


def test_concurrent_accounts_cannot_claim_same_receipt(db_engine, account_id):
    rid = add_receipt(db_engine)
    other_account = add_account(db_engine)
    barrier = Barrier(2)

    def assign(aid):
        barrier.wait(timeout=15)
        try:
            return assign_receipts_to_account(db_engine, aid, [rid])
        except ValueError as error:
            assert "another account" in str(error)
            return 0

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(assign, [account_id, other_account])) == 1
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "receipt.scoped")) == 1


def test_populated_reconciliation_migration_round_trip(db_engine, account_id):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    rid = add_receipt(db_engine, account_id)
    tid = add_transaction(db_engine, account_id)
    reconcile_account(db_engine, account_id)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_bank_ingestion")
        assert connection.scalar(text("SELECT count(*) FROM receipts")) == 1
        assert connection.scalar(text("SELECT count(*) FROM bank_transactions")) == 1
        assert connection.scalar(text("SELECT count(*) FROM audit_events")) == 2
        command.upgrade(config, "head")
        command.check(config)
        assert connection.scalar(select(Receipt.account_id).where(Receipt.id == rid)) is None
        assert connection.scalar(select(BankTransaction.id).where(BankTransaction.id == tid)) == tid
    assert not decisions(db_engine)


def test_database_one_to_one_scope_and_audit_constraints(db_engine, account_id):
    r1 = add_receipt(db_engine, account_id)
    t1 = add_transaction(db_engine, account_id)
    reconcile_account(db_engine, account_id)
    r2 = add_receipt(db_engine, account_id)
    t2 = add_transaction(db_engine, account_id)
    other_account = add_account(db_engine)
    other_transaction = add_transaction(db_engine, other_account)
    for rid, tid, aid in ((r1, t2, account_id), (r2, t1, account_id),
                          (r2, other_transaction, account_id), (r2, t2, other_account)):
        with pytest.raises(IntegrityError):
            with db_engine.begin() as connection:
                connection.execute(insert(ReconciliationDecision).values(
                    receipt_id=rid, account_id=aid, bank_transaction_id=tid,
                    status="matched", revision=1, details={}))
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(insert(AuditEvent).values(receipt_id=r1,
                event_type="receipt.reconciliation_decided", payload={"revision": 1}))


def test_empty_and_unknown_account(db_engine, account_id):
    result = reconcile_account(db_engine, account_id)
    assert result.matched == result.unmatched == result.decisions_written == 0
    with pytest.raises(ValueError, match="existing account"):
        reconcile_account(db_engine, uuid4())
    with pytest.raises(ValueError, match="existing receipts"):
        assign_receipts_to_account(db_engine, account_id, [uuid4()])
