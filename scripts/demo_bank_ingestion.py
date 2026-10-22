"""Import the sample CSV twice and prove transaction/audit counts stay stable."""
import os
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger.persistence.bank_ingest import import_bank_transactions
from ledger.persistence.models import Account, AuditEvent, BankTransaction


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    account_id = uuid5(NAMESPACE_URL, "ledger:local-demo:bank-account")
    try:
        # Account provisioning is separate from CSV ingestion. A stable demo
        # UUID lets repeated script runs reuse the same account and identities.
        with engine.begin() as connection:
            connection.execute(insert(Account).values(id=account_id, name="Local demo checking")
                               .on_conflict_do_nothing(index_elements=[Account.id]))
        content = (ROOT / "data/samples/bank-transactions.csv").read_text(encoding="utf-8")

        def counts():
            with engine.connect() as connection:
                transactions = connection.scalar(select(func.count()).select_from(BankTransaction).where(
                    BankTransaction.account_id == account_id))
                events = connection.scalar(select(func.count()).select_from(AuditEvent).join(
                    BankTransaction, AuditEvent.bank_transaction_id == BankTransaction.id).where(
                    BankTransaction.account_id == account_id, AuditEvent.event_type == "bank_transaction.created"))
                return transactions, events

        print(f"Account: {account_id}")
        first = import_bank_transactions(engine, account_id, content)
        first_counts = counts()
        print(f"First:  inserted={first.inserted}, skipped={first.skipped}; "
              f"transactions={first_counts[0]}, creation_audit_events={first_counts[1]}")
        second = import_bank_transactions(engine, account_id, content)
        second_counts = counts()
        print(f"Second: inserted={second.inserted}, skipped={second.skipped}; "
              f"transactions={second_counts[0]}, creation_audit_events={second_counts[1]}")
        assert second.inserted == 0 and second.skipped == 3
        assert first_counts == second_counts == (3, 3)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
