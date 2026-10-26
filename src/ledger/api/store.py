"""Small HTTP storage adapter; service writes retain their own transactions."""
from sqlalchemy import insert, or_, select

from ledger.persistence.models import Account, AuditEvent, BankTransaction, Receipt, ReconciliationDecision


class MissingRecord(Exception):
    pass


class ScopeConflict(Exception):
    pass


class Store:
    def __init__(self, engine):
        self.engine = engine

    def require(self, model, record_id, account_id=None):
        statement = select(model.__table__).where(model.id == record_id)
        if account_id is not None:
            statement = statement.where(model.account_id == account_id)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise MissingRecord(f"{model.__name__} not found in the selected scope")
        return row

    def account(self, account_id):
        self.require(Account, account_id)

    def create_account(self, name):
        with self.engine.begin() as connection:
            return dict(connection.execute(insert(Account).values(name=name)
                        .returning(*Account.__table__.c)).mappings().one())

    def page(self, model, limit, offset, account_id=None):
        statement = select(model.__table__)
        if account_id is not None:
            statement = statement.where(model.account_id == account_id)
        with self.engine.connect() as connection:
            return connection.execute(statement.order_by(model.created_at, model.id)
                                      .limit(limit).offset(offset)).mappings().all()

    def assignment(self, account_id, receipt_ids):
        self.account(account_id)
        for receipt_id in receipt_ids:
            row = self.require(Receipt, receipt_id)
            if row["account_id"] not in (None, account_id):
                raise ScopeConflict("receipt is already assigned to another account")

    def review(self, account_id, receipt_id, transaction_id):
        self.account(account_id)
        self.require(Receipt, receipt_id, account_id)
        self.require(BankTransaction, transaction_id, account_id)
        with self.engine.connect() as connection:
            if connection.scalar(select(ReconciliationDecision.id).where(
                    ReconciliationDecision.account_id == account_id,
                    ReconciliationDecision.receipt_id == receipt_id)) is None:
                raise MissingRecord("receipt has no reconciliation case")

    def audit(self, account_id, limit, offset):
        # Include creation events before assignment, scoped via current membership.
        statement = select(AuditEvent.__table__).where(or_(
            AuditEvent.receipt_id.in_(select(Receipt.id).where(Receipt.account_id == account_id)),
            AuditEvent.bank_transaction_id.in_(select(BankTransaction.id).where(
                BankTransaction.account_id == account_id))))
        with self.engine.connect() as connection:
            return connection.execute(statement.order_by(AuditEvent.created_at, AuditEvent.id)
                                      .limit(limit).offset(offset)).mappings().all()
