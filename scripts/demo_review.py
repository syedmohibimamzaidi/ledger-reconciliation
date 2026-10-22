"""Human confirmation and per-candidate rejection survive reconciliation."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.ingest import StructuredReceipt, ingest_receipt
from ledger.persistence.models import Account, AuditEvent, BankTransaction, ReconciliationDecision
from ledger.persistence.reconcile import assign_receipts_to_account, reconcile_account
from ledger.persistence.review import confirm_candidate, list_review_cases, reject_candidate


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    aid = uuid5(NAMESPACE_URL, "ledger:local-demo:human-review")
    try:
        with engine.begin() as connection:
            connection.execute(insert(Account).values(id=aid, name="Human review demo")
                               .on_conflict_do_nothing(index_elements=[Account.id]))
        rids = {}
        for label, merchant, amount in (("confirm", "Harbor Review", "20.00"), ("reject", "Cedar Review", "12.30")):
            rids[label] = ingest_receipt(engine, StructuredReceipt(
                merchant, date(2026, 10, 22), Decimal(amount), "CAD", "human-review-demo", label)).receipt_id
        assign_receipts_to_account(engine, aid, list(rids.values()))
        import_bank_transactions(engine, aid, """source_transaction_id,date,merchant,amount,currency
confirm-001,2026-10-23,Harbor Review,-20.00,CAD
confirm-002,2026-10-23,Harbor Review,-20.00,CAD
reject-001,2026-10-23,Cedar Review,-12.30,CAD
reject-002,2026-10-23,Cedar Review,-12.30,CAD
""")
        reconcile_account(engine, aid)
        print(f"Cases awaiting review before actions: {len(list_review_cases(engine, aid))}")
        with engine.connect() as connection:
            tids = {row.source_transaction_id: row.id for row in connection.execute(
                select(BankTransaction.id, BankTransaction.source_transaction_id).where(BankTransaction.account_id == aid))}
        confirmed = confirm_candidate(engine, aid, rids["confirm"], tids["confirm-001"], expected_revision=1,
            request_id=uuid5(NAMESPACE_URL, "ledger:local-demo:review-confirm-request"),
            reviewer="local-demo-reviewer", reason="Verified statement reference against receipt")
        reject_candidate(engine, aid, rids["reject"], tids["reject-001"], expected_revision=1,
            request_id=uuid5(NAMESPACE_URL, "ledger:local-demo:review-reject-request"),
            reviewer="local-demo-reviewer", reason="Duplicate authorization, not the receipt payment")
        rerun = reconcile_account(engine, aid)
        with engine.connect() as connection:
            saved = connection.execute(select(ReconciliationDecision.__table__).where(
                ReconciliationDecision.receipt_id == rids["confirm"])).one()
            human_events = connection.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.receipt_id == rids["confirm"], AuditEvent.event_type == "receipt.reviewed"))
        review = list_review_cases(engine, aid)[0]
        assert saved.status == "matched" and saved.bank_transaction_id == tids["confirm-001"]
        assert human_events == 1 and rerun.decisions_written == 0
        assert [candidate["bank_transaction_id"] for candidate in review.candidates] == [str(tids["reject-002"])]
        assert review.rejected_candidates[0]["bank_transaction_id"] == str(tids["reject-001"])
        print(f"Confirmed case after rerun: {saved.status}, human_action_audit_events={human_events}")
        print("Rejected candidate remains excluded; other candidate remains available for review")
        print(f"Reconciliation decision writes after human actions: {rerun.decisions_written}")
        print(f"Confirmation was an idempotent retry: {confirmed.repeated}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
