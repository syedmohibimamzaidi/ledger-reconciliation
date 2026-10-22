from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ledger.persistence.ingest import IdentityConflict, StructuredReceipt, ingest_receipt
from ledger.persistence.models import AuditEvent, Receipt


def receipt(**changes):
    return replace(StructuredReceipt(
        merchant="Blue Bottle Coffee", receipt_date=date(2026, 10, 22),
        amount=Decimal("12.30"), currency="CAD", source="demo",
        source_receipt_id="receipt-001",
    ), **changes)


def counts(engine):
    with engine.connect() as connection:
        return tuple(connection.scalar(select(func.count()).select_from(model))
                     for model in (Receipt, AuditEvent))


def test_persistence_and_audit_snapshot(db_engine):
    result = ingest_receipt(db_engine, receipt())
    assert result.created
    # A new session/connection must see the committed state and Decimal value.
    with Session(db_engine) as session:
        saved = session.get(Receipt, result.receipt_id)
        assert saved.merchant == "Blue Bottle Coffee"
        assert saved.receipt_date == date(2026, 10, 22)
        assert saved.amount == Decimal("12.30")
        assert isinstance(saved.amount, Decimal)
        assert (saved.currency, saved.source, saved.source_receipt_id) == ("CAD", "demo", "receipt-001")
        assert saved.status == "pending"
        assert saved.created_at.tzinfo is not None
        assert saved.updated_at == saved.created_at
        audit = session.scalars(select(AuditEvent)).one()
        assert audit.receipt_id == saved.id
        assert audit.event_type == "receipt.created"
        assert audit.created_at == saved.created_at
        assert audit.payload == {
            "merchant": "Blue Bottle Coffee", "receipt_date": "2026-10-22",
            "amount": "12.30", "currency": "CAD", "source": "demo",
            "source_receipt_id": "receipt-001", "status": "pending",
        }
    assert counts(db_engine) == (1, 1)


def test_repeated_submission_does_not_change_receipt_or_audit(db_engine):
    first = ingest_receipt(db_engine, receipt())
    with db_engine.connect() as connection:
        before = connection.execute(select(Receipt.__table__)).mappings().one()
        audit_before = connection.execute(select(AuditEvent.__table__)).mappings().one()
    second = ingest_receipt(db_engine, receipt(currency="cad", amount=Decimal("12.300")))
    assert second.receipt_id == first.receipt_id
    assert not second.created
    assert counts(db_engine) == (1, 1)
    with db_engine.connect() as connection:
        assert connection.execute(select(Receipt.__table__)).mappings().one() == before
        assert connection.execute(select(AuditEvent.__table__)).mappings().one() == audit_before


def test_identical_business_fields_with_distinct_source_identity_are_distinct(db_engine):
    results = [ingest_receipt(db_engine, data) for data in (
        receipt(), receipt(source_receipt_id="receipt-002"), receipt(source="another-import"),
    )]
    assert len({result.receipt_id for result in results}) == 3
    assert counts(db_engine) == (3, 3)


@pytest.mark.parametrize("changes", [
    {"merchant": "Other"}, {"amount": Decimal("15.00")},
    {"receipt_date": date(2026, 10, 23)}, {"currency": "USD"},
])
def test_changed_content_for_existing_identity_is_rejected(db_engine, changes):
    first = ingest_receipt(db_engine, receipt())
    with pytest.raises(IdentityConflict):
        ingest_receipt(db_engine, receipt(**changes))
    assert counts(db_engine) == (1, 1)
    assert ingest_receipt(db_engine, receipt()).receipt_id == first.receipt_id


def test_concurrent_duplicate_submissions(db_engine):
    # Hold every connection immediately before INSERT so competing transactions
    # really race on the database constraint, rather than run sequentially.
    barrier = Barrier(4)

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO receipts"):
            barrier.wait(timeout=15)

    event.listen(db_engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: ingest_receipt(db_engine, receipt()), range(4)))
    finally:
        event.remove(db_engine, "before_cursor_execute", synchronize)
    assert len({result.receipt_id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    assert counts(db_engine) == (1, 1)


def test_audit_failure_rolls_back_receipt_and_allows_retry(db_engine):
    # Inject a real PostgreSQL constraint failure on the second write, after
    # INSERT of the receipt has succeeded inside the service transaction.
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events ADD CONSTRAINT force_failure CHECK (false)"))
    with pytest.raises(IntegrityError):
        ingest_receipt(db_engine, receipt())
    assert counts(db_engine) == (0, 0)
    with db_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_events DROP CONSTRAINT force_failure"))
    assert ingest_receipt(db_engine, receipt()).created
    assert counts(db_engine) == (1, 1)


def test_database_rejects_duplicate_identity_without_service(db_engine):
    ingest_receipt(db_engine, receipt())
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(Receipt.__table__.insert().values(**receipt().validated_fields()))
    assert counts(db_engine) == (1, 1)


def test_database_rejects_second_creation_event(db_engine):
    result = ingest_receipt(db_engine, receipt())
    with pytest.raises(IntegrityError):
        with db_engine.begin() as connection:
            connection.execute(AuditEvent.__table__.insert().values(
                receipt_id=result.receipt_id, event_type="receipt.created", payload={},
            ))
    assert counts(db_engine) == (1, 1)


def test_migration_matches_models_and_round_trips(db_engine):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import inspect

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.check(config)
        command.downgrade(config, "base")
        assert "receipts" not in inspect(connection).get_table_names()
        assert "audit_events" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
    assert counts(db_engine) == (0, 0)


@pytest.mark.parametrize("changes", [
    {"merchant": " "}, {"merchant": "x" * 256}, {"merchant": "a\x00b"},
    {"source": ""}, {"source_receipt_id": " "},
    {"receipt_date": "2026-10-22"}, {"receipt_date": datetime(2026, 10, 22)},
    {"amount": 12.30}, {"amount": Decimal("NaN")}, {"amount": Decimal("Infinity")},
    {"amount": Decimal("-0.01")}, {"amount": Decimal("0.001")},
    {"amount": Decimal("10000000000000000")}, {"currency": "CA"}, {"currency": "123"},
])
def test_invalid_input_is_rejected_before_connecting(changes):
    # Validation precedes any engine access.
    with pytest.raises(ValueError):
        ingest_receipt(None, receipt(**changes))


def test_zero_and_maximum_amounts_are_valid():
    assert receipt(amount=Decimal("0")).validated_fields()["amount"] == Decimal("0.00")
    assert receipt(amount=Decimal("9999999999999999.99")).validated_fields()["amount"] == Decimal("9999999999999999.99")
