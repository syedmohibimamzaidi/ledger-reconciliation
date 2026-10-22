"""Submit the same structured receipt twice and inspect its committed rows."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import AuditEvent, Receipt


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        receipt = StructuredReceipt(
            merchant="Blue Bottle Coffee", receipt_date=date(2026, 10, 22),
            amount=Decimal("12.30"), currency="CAD", source="local-demo",
            source_receipt_id="receipt-001",
        )
        first = ingest_receipt(engine, receipt)
        second = ingest_receipt(engine, receipt)
        print(f"First:  {first.receipt_id} created={first.created}")
        print(f"Second: {second.receipt_id} created={second.created}")
        with engine.connect() as connection:
            receipts = connection.scalar(select(func.count()).select_from(Receipt).where(
                Receipt.source == receipt.source, Receipt.source_receipt_id == receipt.source_receipt_id,
            ))
            events = connection.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.receipt_id == first.receipt_id, AuditEvent.event_type == "receipt.created",
            ))
        print(f"Rows for this identity: receipts={receipts}, creation_audit_events={events}")
        assert first.receipt_id == second.receipt_id and (receipts, events) == (1, 1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
