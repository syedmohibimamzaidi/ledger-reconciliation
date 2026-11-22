"""Small HTTP storage adapter; service writes retain their own transactions."""
from sqlalchemy import and_, case, func, insert, or_, select, true

from ledger.persistence.models import Account, AuditEvent, BankTransaction, Receipt, ReconciliationDecision, ReconciliationJob


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

    def page(self, model, limit, offset, account_id=None, *, filters=(), newest=False):
        statement = select(model.__table__)
        if account_id is not None:
            statement = statement.where(model.account_id == account_id)
        statement = statement.where(*filters)
        order = (model.created_at.desc(), model.id.desc()) if newest else (model.created_at, model.id)
        with self.engine.connect() as connection:
            return connection.execute(statement.order_by(*order)
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

    def audit(self, account_id, limit, offset, *, newest=False):
        # Include creation events before assignment, scoped via current membership.
        statement = select(AuditEvent.__table__).where(or_(
            AuditEvent.receipt_id.in_(select(Receipt.id).where(Receipt.account_id == account_id)),
            AuditEvent.bank_transaction_id.in_(select(BankTransaction.id).where(
                BankTransaction.account_id == account_id))))
        order = (AuditEvent.created_at.desc(), AuditEvent.id.desc()) if newest else (AuditEvent.created_at, AuditEvent.id)
        with self.engine.connect() as connection:
            return connection.execute(statement.order_by(*order)
                                      .limit(limit).offset(offset)).mappings().all()


    def receipt_detail(self, receipt_id):
        decision = ReconciliationDecision.__table__
        statement = select(*Receipt.__table__.c, case(
            (decision.c.id.is_not(None), func.row_to_json(decision.table_valued())),
            else_=None).label("decision")).select_from(
                Receipt.__table__.outerjoin(decision, decision.c.receipt_id == Receipt.id)
            ).where(Receipt.id == receipt_id)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise MissingRecord("Receipt not found")
        return row

    def jobs(self, account_id, limit, offset, status=None):
        display = job_display_status()
        statement = select(*ReconciliationJob.__table__.c, display.label("display_status")).where(
            ReconciliationJob.account_id == account_id)
        if status is not None:
            statement = statement.where(display == status)
        with self.engine.connect() as connection:
            return connection.execute(statement.order_by(ReconciliationJob.created_at.desc(),
                ReconciliationJob.id.desc()).limit(limit).offset(offset)).mappings().all()

    def summary(self, account_id):
        """One SELECT/one MVCC snapshot, without account locks or service calls."""
        r, d, b, j = Receipt, ReconciliationDecision, BankTransaction, ReconciliationJob
        receipt_counts = select(
            func.count().label("total"),
            func.count().filter(d.status == "matched").label("matched"),
            func.count().filter(d.status == "needs_review").label("needs_review"),
            func.count().filter(d.status == "unmatched").label("unmatched"),
            func.count().filter(d.id.is_(None)).label("without_decision"),
        ).select_from(r.__table__.outerjoin(d.__table__, d.receipt_id == r.id)).where(
            r.account_id == account_id).subquery("receipt_counts")
        transaction_counts = select(
            func.count().label("total"),
            func.count().filter(and_(b.amount < 0, d.id.is_not(None))).label("matched_debits"),
            func.count().filter(and_(b.amount < 0, d.id.is_(None))).label("unmatched_debits"),
            func.count().filter(b.amount > 0).label("credits_not_eligible"),
            func.count().filter(b.amount == 0).label("zero_amount_not_eligible"),
        ).select_from(b.__table__.outerjoin(d.__table__, and_(d.bank_transaction_id == b.id,
            d.status == "matched"))).where(b.account_id == account_id).subquery("transaction_counts")
        display = job_display_status()
        job_counts = select(*[func.count().filter(display == status).label(status)
            for status in ("queued", "retrying", "running")]).where(
                j.account_id == account_id).subquery("job_counts")
        # Only public job fields enter the JSON; fencing tokens/payload stay internal.
        latest_job = select(*[column for column in j.__table__.c if column.name not in (
            "attempt_token", "request_payload")], display.label("display_status")).where(
                j.account_id == account_id).order_by(j.created_at.desc(), j.id.desc()).limit(1).subquery("latest_job")
        assigned_since = select(func.count()).select_from(r).join(AuditEvent, and_(
            AuditEvent.receipt_id == r.id, AuditEvent.event_type == "receipt.scoped")).where(
            r.account_id == account_id, or_(Account.last_reconciled_at.is_(None),
                AuditEvent.created_at > Account.last_reconciled_at)).correlate(Account).scalar_subquery()
        imported_since = select(func.count()).select_from(b).where(b.account_id == account_id,
            or_(Account.last_reconciled_at.is_(None), b.created_at > Account.last_reconciled_at)
        ).correlate(Account).scalar_subquery()
        statement = select(Account.id.label("account_id"), Account.last_reconciled_at,
            *[column.label("receipts_" + column.name) for column in receipt_counts.c],
            *[column.label("transactions_" + column.name) for column in transaction_counts.c],
            *[column.label("jobs_" + column.name) for column in job_counts.c],
            assigned_since.label("assigned_since_last_run"), imported_since.label("imported_since_last_run"),
            case((latest_job.c.id.is_not(None), func.row_to_json(latest_job.table_valued())),
                 else_=None).label("latest_job"),
        ).select_from(Account).join(receipt_counts, true()).join(transaction_counts, true()).join(
            job_counts, true()).outerjoin(latest_job, true()).where(Account.id == account_id)
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise MissingRecord("Account not found")
        receipts = {key.removeprefix("receipts_"): value for key, value in row.items() if key.startswith("receipts_")}
        transactions = {key.removeprefix("transactions_"): value for key, value in row.items() if key.startswith("transactions_")}
        jobs = {key.removeprefix("jobs_"): value for key, value in row.items() if key.startswith("jobs_")}
        receipts["assigned_since_last_run"] = row["assigned_since_last_run"]
        transactions["imported_since_last_run"] = row["imported_since_last_run"]
        return {"account_id": row["account_id"], "last_reconciled_at": row["last_reconciled_at"],
            "receipts": receipts, "transactions": transactions, "active_jobs": jobs,
            "latest_job": row["latest_job"], "has_new_reconciliation_inputs":
                row["assigned_since_last_run"] > 0 or row["imported_since_last_run"] > 0}


def job_display_status():
    return case((and_(ReconciliationJob.status == "queued", ReconciliationJob.attempt_count > 0,
        ReconciliationJob.next_attempt_at > func.statement_timestamp()), "retrying"),
        else_=ReconciliationJob.status)
