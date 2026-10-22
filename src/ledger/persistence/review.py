"""Revision-checked, idempotent human decisions on reconciliation candidates."""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, insert, select, update

from .models import Account, AuditEvent, ReconciliationDecision, ReviewAction
from .reconcile import _load_account_state, _load_rejections, decide, is_plausible


class ReviewConflict(RuntimeError):
    """A review cannot be applied to the current state; reload the case."""


class StaleReview(ReviewConflict):
    def __init__(self, expected: int, actual: int):
        self.expected, self.actual = expected, actual
        super().__init__(f"stale case revision: expected {expected}, current {actual}")


@dataclass(frozen=True)
class ReviewCase:
    account_id: UUID
    receipt_id: UUID
    revision: int
    reasons: tuple[str, ...]
    receipt: dict
    candidates: tuple[dict, ...]
    rejected_candidates: tuple[dict, ...]


@dataclass(frozen=True)
class ReviewResult:
    action_id: UUID
    receipt_id: UUID
    revision: int
    status: str
    reviewed_at: datetime
    repeated: bool


def _lock_account(connection, account_id):
    if connection.scalar(select(Account.id).where(Account.id == account_id).with_for_update()) is None:
        raise ValueError("account_id does not identify an existing account")


def _validate_engine_account(engine, account_id):
    if not isinstance(account_id, UUID):
        raise ValueError("account_id must be a UUID")
    if engine.dialect.name != "postgresql":
        raise ValueError("human review requires PostgreSQL")


def list_review_cases(engine: Engine, account_id: UUID) -> list[ReviewCase]:
    """Return persisted needs_review snapshots and their expected revisions.

    Reconcile first to populate/refresh cases. Listing never changes outcomes.
    Account locking gives a coherent view with concurrent review/reconciliation.
    """
    _validate_engine_account(engine, account_id)
    with engine.begin() as connection:
        _lock_account(connection, account_id)
        rejections = {}
        for row in connection.execute(select(ReviewAction.__table__).where(
                ReviewAction.account_id == account_id, ReviewAction.action == "reject").order_by(ReviewAction.created_at, ReviewAction.id)):
            rejections.setdefault(row.receipt_id, []).append({
                "bank_transaction_id": str(row.bank_transaction_id), "reviewer": row.reviewer,
                "reason": row.reason, "reviewed_at": row.created_at.isoformat(),
            })
        cases = [ReviewCase(
            account_id, row.receipt_id, row.revision, tuple(row.details["reasons"]),
            row.details["receipt"], tuple(row.details["candidates"]), tuple(rejections.get(row.receipt_id, [])),
        ) for row in connection.execute(select(ReconciliationDecision.__table__).where(
            ReconciliationDecision.account_id == account_id, ReconciliationDecision.status == "needs_review")
            .order_by(ReconciliationDecision.receipt_id))]
    return cases


def confirm_candidate(engine: Engine, account_id: UUID, receipt_id: UUID, bank_transaction_id: UUID,
                      *, expected_revision: int, request_id: UUID, reviewer: str, reason: str,
                      acknowledge_amount_mismatch: bool = False) -> ReviewResult:
    return _apply_review(engine, account_id, receipt_id, bank_transaction_id,
                         "confirm", expected_revision, request_id, reviewer, reason, acknowledge_amount_mismatch)


def reject_candidate(engine: Engine, account_id: UUID, receipt_id: UUID, bank_transaction_id: UUID,
                     *, expected_revision: int, request_id: UUID, reviewer: str, reason: str) -> ReviewResult:
    return _apply_review(engine, account_id, receipt_id, bank_transaction_id,
                         "reject", expected_revision, request_id, reviewer, reason, False)


def _apply_review(engine, account_id, receipt_id, bank_transaction_id, action,
                  expected_revision, request_id, reviewer, reason, acknowledge_amount_mismatch):
    if any(not isinstance(value, UUID) for value in (account_id, receipt_id, bank_transaction_id, request_id)):
        raise ValueError("account, receipt, transaction and request IDs must be UUIDs")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    if type(acknowledge_amount_mismatch) is not bool:
        raise ValueError("acknowledge_amount_mismatch must be an explicit boolean")
    for label, value, limit in (("reviewer", reviewer, 255), ("reason", reason, 2000)):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or "\x00" in value:
            raise ValueError(f"{label} must be nonblank, contain no NUL, and have at most {limit} characters")
    reviewer, reason = reviewer.strip(), reason.strip()
    _validate_engine_account(engine, account_id)
    request = {"receipt_id": receipt_id, "bank_transaction_id": bank_transaction_id, "action": action,
               "expected_revision": expected_revision, "reviewer": reviewer, "reason": reason,
               "acknowledge_amount_mismatch": acknowledge_amount_mismatch}
    with engine.begin() as connection:
        _lock_account(connection, account_id)
        prior = connection.execute(select(ReviewAction.__table__).where(
            ReviewAction.account_id == account_id, ReviewAction.request_id == request_id)).mappings().one_or_none()
        if prior:
            if any(prior[key] != value for key, value in request.items()):
                raise ReviewConflict("request_id already used with different review input")
            return ReviewResult(prior["id"], receipt_id, prior["resulting_revision"],
                                prior["resulting_status"], prior["created_at"], repeated=True)
        receipts, transactions, previous = _load_account_state(connection, account_id)
        case = previous.get(receipt_id)
        if case is None:
            raise ValueError("receipt has no reconciliation case in the selected account")
        if case.revision != expected_revision:
            raise StaleReview(expected_revision, case.revision)
        if case.status != "needs_review":
            raise ReviewConflict("case is not awaiting review; accepted matches cannot be changed")
        receipt = next(row for row in receipts if row.id == receipt_id)
        transaction = next((row for row in transactions if row.id == bank_transaction_id), None)
        if transaction is None:
            raise ValueError("candidate does not belong to the selected account")
        rejected = _load_rejections(connection, account_id)
        if (receipt_id, bank_transaction_id) in rejected:
            raise ReviewConflict("candidate was already rejected")
        candidate_ids = {item["bank_transaction_id"] for item in case.details["candidates"]}
        if str(bank_transaction_id) not in candidate_ids or not is_plausible(receipt, transaction):
            raise ValueError("transaction is not an eligible candidate for this case")
        accepted = {rid: row.bank_transaction_id for rid, row in previous.items() if row.status == "matched"}
        if action == "confirm" and bank_transaction_id in accepted.values():
            raise ReviewConflict("transaction is already accepted for another receipt")
        mismatch = receipt.amount != transaction.amount.copy_negate()
        if action == "confirm" and mismatch and not acknowledge_amount_mismatch:
            raise ValueError("confirming an amount mismatch requires explicit acknowledgement and a reason")
        action_id = uuid4()
        revision = case.revision + 1
        snapshot = {"receipt": {"merchant": receipt.merchant, "date": receipt.date.isoformat(),
                                "amount": format(receipt.amount, ".2f"), "currency": receipt.currency},
                    "transaction": {"merchant": transaction.merchant, "date": transaction.date.isoformat(),
                                    "amount": format(transaction.amount, ".2f"), "currency": transaction.currency}}
        if action == "confirm":
            status, chosen = "matched", bank_transaction_id
            details = {**case.details, "reasons": ["human_confirmed"], "confirmation": {
                "action_id": str(action_id), "reviewer": reviewer, "reason": reason,
                "acknowledge_amount_mismatch": acknowledge_amount_mismatch, **snapshot}}
        else:
            rejected.add((receipt_id, bank_transaction_id))
            outcome = decide(receipts, transactions, accepted, rejected)[receipt_id]
            status, chosen, details = outcome.status, None, outcome.details
        reviewed_at = connection.execute(insert(ReviewAction).values(
            id=action_id, request_id=request_id, account_id=account_id, **request,
            resulting_revision=revision, resulting_status=status, receipt_amount=receipt.amount,
            bank_amount=transaction.amount, snapshot=snapshot,
        ).returning(ReviewAction.created_at)).scalar_one()
        connection.execute(update(ReconciliationDecision).where(ReconciliationDecision.id == case.id).values(
            status=status, bank_transaction_id=chosen, details=details, revision=revision, updated_at=func.now()))
        connection.execute(insert(AuditEvent).values(
            receipt_id=receipt_id, review_action_id=action_id, event_type="receipt.reviewed",
            payload={"action_id": str(action_id), "request_id": str(request_id), "account_id": str(account_id),
                     "bank_transaction_id": str(bank_transaction_id), "action": action, "reviewer": reviewer,
                     "reason": reason, "reviewed_at": reviewed_at.isoformat(), "revision": revision,
                     "expected_revision": expected_revision, "status": status,
                     "acknowledge_amount_mismatch": acknowledge_amount_mismatch,
                     "decision": details, **snapshot}))
        result = ReviewResult(action_id, receipt_id, revision, status, reviewed_at, repeated=False)
    return result
