from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from threading import Barrier, local
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ledger.persistence.bank_ingest import (
    CSV_COLUMNS, CSVValidationError, ImportSummary, TransactionIdentityConflict,
    import_bank_transactions,
)
from ledger.persistence.models import Account, AuditEvent, BankTransaction


def csv_text(*rows):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue()


DEBIT = ("bank-001", "2026-10-22", "Blue Bottle Coffee", "-12.30", "CAD")
CREDIT = ("bank-002", "2026-10-23", "Refund", "+4.50", "CAD")


@pytest.fixture
def account_id(db_engine):
    with Session(db_engine) as session, session.begin():
        account = Account(name="Demo checking")
        session.add(account)
        session.flush()
        return account.id


def counts(engine):
    with engine.connect() as connection:
        return (connection.scalar(select(func.count()).select_from(BankTransaction)),
                connection.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.event_type == "bank_transaction.created")))


def test_committed_bank_persistence_and_audit(db_engine, account_id):
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, CREDIT)) == ImportSummary(2, 0)
    with Session(db_engine) as session:
        transactions = session.scalars(select(BankTransaction).order_by(BankTransaction.source_transaction_id)).all()
        assert len(transactions) == 2
        debit, credit = transactions
        assert debit.account_id == account_id
        assert debit.source_transaction_id == "bank-001"
        assert debit.merchant == "Blue Bottle Coffee"
        assert debit.transaction_date == date(2026, 10, 22)
        assert isinstance(debit.amount, Decimal)
        assert debit.amount == Decimal("-12.30")
        assert credit.amount == Decimal("4.50")
        assert debit.currency == "CAD"
        assert debit.created_at.tzinfo is not None
        events = session.scalars(select(AuditEvent)).all()
        assert len(events) == 2
        audit = next(item for item in events if item.bank_transaction_id == debit.id)
        assert audit.receipt_id is None
        assert audit.created_at == debit.created_at
        assert audit.payload == {
            "account_id": str(account_id), "source_transaction_id": "bank-001",
            "transaction_date": "2026-10-22", "merchant": "Blue Bottle Coffee",
            "amount": "-12.30", "currency": "CAD",
        }
    assert counts(db_engine) == (2, 2)


def test_reimport_preserves_rows_ids_and_timestamps(db_engine, account_id):
    content = csv_text(DEBIT, CREDIT)
    import_bank_transactions(db_engine, account_id, content)
    with db_engine.connect() as connection:
        before = connection.execute(select(BankTransaction.__table__).order_by(BankTransaction.id)).all()
        audit_before = connection.execute(select(AuditEvent.__table__).order_by(AuditEvent.id)).all()
    assert import_bank_transactions(db_engine, account_id, content) == ImportSummary(0, 2)
    with db_engine.connect() as connection:
        assert connection.execute(select(BankTransaction.__table__).order_by(BankTransaction.id)).all() == before
        assert connection.execute(select(AuditEvent.__table__).order_by(AuditEvent.id)).all() == audit_before
    assert counts(db_engine) == (2, 2)


def test_same_business_fields_different_ids_and_accounts_are_distinct(db_engine, account_id):
    other_payment = ("bank-003", *DEBIT[1:])
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, other_payment)) == ImportSummary(2, 0)
    with Session(db_engine) as session, session.begin():
        other_account = Account(name="Other account")
        session.add(other_account)
        session.flush()
        other_id = other_account.id
    assert import_bank_transactions(db_engine, other_id, csv_text(DEBIT)) == ImportSummary(1, 0)
    assert counts(db_engine) == (3, 3)


def test_identical_rows_in_one_csv_count_as_skipped(db_engine, account_id):
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, DEBIT)) == ImportSummary(1, 1)
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, DEBIT)) == ImportSummary(0, 2)
    assert counts(db_engine) == (1, 1)


@pytest.mark.parametrize("index,value", [(1, "2026-10-24"), (2, "Other"), (3, "12.30"), (4, "USD")])
def test_conflicting_existing_identity_rolls_back_whole_import(db_engine, account_id, index, value):
    import_bank_transactions(db_engine, account_id, csv_text(CREDIT))
    conflict = list(CREDIT)
    conflict[index] = value
    # bank-001 is inserted first; conflict on bank-002 must undo it and its event.
    with pytest.raises(TransactionIdentityConflict) as caught:
        import_bank_transactions(db_engine, account_id, csv_text(DEBIT, conflict))
    assert caught.value.row == 3
    assert caught.value.source_transaction_id == "bank-002"
    assert counts(db_engine) == (1, 1)
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, CREDIT)) == ImportSummary(1, 1)


def test_concurrent_reversed_duplicate_files(db_engine, account_id):
    barrier, state = Barrier(4), local()

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO bank_transactions") and not getattr(state, "started", False):
            state.started = True
            barrier.wait(timeout=15)

    event.listen(db_engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            contents = [csv_text(DEBIT, CREDIT), csv_text(CREDIT, DEBIT)] * 2
            results = list(executor.map(lambda content: import_bank_transactions(db_engine, account_id, content), contents))
    finally:
        event.remove(db_engine, "before_cursor_execute", synchronize)
    assert sorted((result.inserted, result.skipped) for result in results) == [(0, 2)] * 3 + [(2, 0)]
    assert counts(db_engine) == (2, 2)


def test_invalid_later_rows_leave_no_partial_import(db_engine, account_id):
    invalid_date = ("bad-date", "2026-02-30", "Merchant", "2.00", "CAD")
    invalid_amount = ("bad-amount", "2026-10-22", "Merchant", "NaN", "12")
    with pytest.raises(CSVValidationError) as caught:
        import_bank_transactions(db_engine, account_id, csv_text(DEBIT, invalid_date, invalid_amount))
    assert [error.row for error in caught.value.errors] == [3, 4]
    assert "date" in caught.value.errors[0].message
    assert "amount" in caught.value.errors[1].message
    assert "currency" in caught.value.errors[1].message
    assert counts(db_engine) == (0, 0)


def test_audit_failure_rolls_back_all_new_transactions(db_engine, account_id):
    # Fail only the SECOND event, after a complete first transaction/event pair.
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_bank_failure "
                                "CHECK (payload->>'source_transaction_id' <> 'bank-002')"))
    with pytest.raises(IntegrityError):
        import_bank_transactions(db_engine, account_id, csv_text(DEBIT, CREDIT))
    assert counts(db_engine) == (0, 0)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events DROP CONSTRAINT force_bank_failure"))
    assert import_bank_transactions(db_engine, account_id, csv_text(DEBIT, CREDIT)) == ImportSummary(2, 0)


def test_unknown_account_rejected(db_engine):
    with pytest.raises(ValueError, match="existing account"):
        import_bank_transactions(db_engine, uuid4(), csv_text(DEBIT))
    assert counts(db_engine) == (0, 0)


def test_constraints_enforced_without_import_service(db_engine, account_id):
    import_bank_transactions(db_engine, account_id, csv_text(DEBIT))
    with Session(db_engine) as session:
        saved = session.scalars(select(BankTransaction)).one()
        transaction_id = saved.id
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(BankTransaction.__table__.insert().values(
                account_id=account_id, source_transaction_id=DEBIT[0],
                transaction_date=date(2026, 10, 22), merchant=DEBIT[2],
                amount=Decimal(DEBIT[3]), currency="CAD"))
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(AuditEvent.__table__.insert().values(
                bank_transaction_id=transaction_id, event_type="bank_transaction.created", payload={}))
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(AuditEvent.__table__.insert().values(event_type="bank_transaction.created", payload={}))
    assert counts(db_engine) == (1, 1)


def test_header_only_file_is_empty_import(db_engine, account_id):
    assert import_bank_transactions(db_engine, account_id, csv_text()) == ImportSummary(0, 0)
    assert counts(db_engine) == (0, 0)


@pytest.mark.parametrize("index,value", [
    (0, " "), (0, "x" * 256), (2, ""), (2, "x" * 256), (2, "a\x00b"),
    (1, "20261022"), (1, "2026-2-03"), (1, "2026-13-01"),
    (3, "1.001"), (3, "1e2"), (3, "NaN"), (3, "Infinity"), (3, ""),
    (3, "10000000000000000"), (3, "-10000000000000000"), (4, "12"),
])
def test_field_validation_before_database_access(index, value):
    invalid = list(DEBIT)
    invalid[index] = value
    with pytest.raises(CSVValidationError, match="row 3"):
        import_bank_transactions(None, uuid4(), csv_text(CREDIT, invalid))


@pytest.mark.parametrize("content", [
    "", "date,merchant,amount,currency\n", ",".join(CSV_COLUMNS) + ",date\n",
    ",".join(CSV_COLUMNS) + "\nonly-one-field\n",
    ",".join(CSV_COLUMNS) + '\n"unclosed',
    ",".join(CSV_COLUMNS) + "\n\n",
    csv_text(DEBIT, (DEBIT[0], "2026-10-23", *DEBIT[2:])),
])
def test_malformed_csv_and_infile_conflicts_before_database_access(content):
    with pytest.raises(CSVValidationError, match="row "):
        import_bank_transactions(None, uuid4(), content)


def test_sample_csv_imports_twice(db_engine, account_id):
    sample = (Path(__file__).resolve().parents[1] / "data/samples/bank-transactions.csv").read_text()
    assert import_bank_transactions(db_engine, account_id, sample) == ImportSummary(3, 0)
    assert import_bank_transactions(db_engine, account_id, sample) == ImportSummary(0, 3)
    assert counts(db_engine) == (3, 3)


def test_csv_quoting_normalization_and_numeric_boundaries(db_engine, account_id):
    rows = [
        ("normalized", "2026-10-22", " Café, Main Street\nBranch ", "-0.00", " cad "),
        ("max", "2026-10-22", "Maximum", "9999999999999999.99", "CAD"),
        ("min", "2026-10-22", "Minimum", "-9999999999999999.99", "CAD"),
    ]
    content = "\ufeff" + csv_text(*rows)
    assert import_bank_transactions(db_engine, account_id, content) == ImportSummary(3, 0)
    with Session(db_engine) as session:
        saved = session.scalars(select(BankTransaction).where(
            BankTransaction.source_transaction_id == "normalized")).one()
        assert saved.merchant == "Café, Main Street\nBranch"
        assert saved.currency == "CAD" and saved.amount == Decimal("0.00")
    assert import_bank_transactions(db_engine, account_id, csv_text(
        ("normalized", "2026-10-22", "Café, Main Street\nBranch", "+0", "CAD"))) == ImportSummary(0, 1)


def test_bank_migration_preserves_receipt_data(db_engine, account_id):
    from alembic import command
    from alembic.config import Config
    from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
    from ledger.persistence.models import Receipt

    result = ingest_receipt(db_engine, StructuredReceipt(
        merchant="Receipt", receipt_date=date(2026, 10, 22), amount=Decimal("12.30"),
        currency="CAD", source="test", source_receipt_id="keep-me"))
    import_bank_transactions(db_engine, account_id, csv_text(DEBIT))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_receipt_ingestion")
        assert connection.scalar(select(func.count()).select_from(Receipt)) == 1
        assert connection.scalar(text("SELECT count(*) FROM audit_events")) == 1
        command.upgrade(config, "head")
        command.check(config)
    with Session(db_engine) as session:
        assert session.get(Receipt, result.receipt_id) is not None
        audit = session.scalars(select(AuditEvent)).one()
        assert audit.receipt_id == result.receipt_id
        assert audit.bank_transaction_id is None
