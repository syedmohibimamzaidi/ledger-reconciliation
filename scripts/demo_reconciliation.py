"""Demonstrate clear, mismatched, ambiguous, and unmatched stored receipts."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, ReconciliationDecision
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    account_id = uuid5(NAMESPACE_URL, "ledger:local-demo:reconciliation-account")
    try:
        with engine.begin() as connection:
            connection.execute(insert(Account).values(id=account_id, name="Reconciliation demo")
                               .on_conflict_do_nothing(index_elements=[Account.id]))
        labels = {}
        for label, merchant, amount in (
            ("clear", "Harbor Books", "20.00"),
            ("amount mismatch", "Cedar Cafe", "12.30"),
            ("ambiguous", "North Market", "30.00"),
            ("unmatched", "Lantern Bakery", "8.50"),
        ):
            rid = ingest_receipt(engine, StructuredReceipt(
                merchant, date(2026, 10, 22), Decimal(amount), "CAD", "reconciliation-demo", label)).receipt_id
            labels[rid] = label
        assign_receipts_to_account(engine, account_id, list(labels))
        import_bank_transactions(engine, account_id, """source_transaction_id,date,merchant,amount,currency
clear-001,2026-10-23,SQ *HARBOR BOOKS,-20.00,CAD
mismatch-001,2026-10-23,Cedar Cafe,-15.00,CAD
ambiguous-001,2026-10-23,North Market,-30.00,CAD
ambiguous-002,2026-10-23,North Market,-30.00,CAD
""")
        first = reconcile_account(engine, account_id)
        with engine.connect() as connection:
            rows = connection.execute(select(ReconciliationDecision.__table__).where(
                ReconciliationDecision.account_id == account_id)).all()
        for row in sorted(rows, key=lambda item: labels[item.receipt_id]):
            print(f"{labels[row.receipt_id]:<16} {row.status:<12} {', '.join(row.details['reasons'])}")
        second = reconcile_account(engine, account_id)
        print(f"Summary: matched={first.matched}, needs_review={first.needs_review}, unmatched={first.unmatched}")
        print(f"Decision writes: first={first.decisions_written}, repeated={second.decisions_written}")
        assert (first.matched, first.needs_review, first.unmatched) == (1, 2, 1)
        assert second.decisions_written == 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
