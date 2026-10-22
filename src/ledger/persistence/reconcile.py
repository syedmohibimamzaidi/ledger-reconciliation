"""Conservative, account-scoped reconciliation; no greedy score-based booking."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
import unicodedata
from uuid import UUID

from sqlalchemy import Engine, func, insert, select, update

from ledger.baseline.match import normalize_merchant
from .models import Account, AuditEvent, BankTransaction, Receipt, ReconciliationDecision

RULES_VERSION = "conservative-v1"
MAX_SETTLEMENT_DAYS = 4
# Same processor prefixes recognized by the baseline; retain meaningful merchant
# tokens rather than using its lossy normalization as an automatic identity.
PROCESSOR_PREFIX = re.compile(r"^(SQ \*|TST\* ?|PP\*|SP |PY \*|IC\* ?)")


def merchant_key(merchant: str) -> tuple[str, tuple[str, ...]]:
    merchant = PROCESSOR_PREFIX.sub("", unicodedata.normalize("NFKC", merchant).upper().strip())
    tokens = tuple(re.findall(r"[^\W_]+", merchant))
    return normalize_merchant(merchant), tokens


@dataclass(frozen=True)
class Record:
    id: UUID
    merchant: str
    date: date
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Outcome:
    status: str
    bank_transaction_id: UUID | None
    details: dict


def decide(receipts: list[Record], transactions: list[Record],
           accepted: dict[UUID, UUID] | None = None) -> dict[UUID, Outcome]:
    """Pure rules over the full account graph, including previously matched rows.

    Amount is intentionally not a candidate filter. A different amount can be a
    tip, extraction error, or separate payment and must not disappear as evidence.
    """
    accepted = accepted or {}
    used_transactions = set(accepted.values())
    candidates = {receipt.id: [] for receipt in receipts}
    competitors = {transaction.id: [] for transaction in transactions}
    for receipt in receipts:
        key = merchant_key(receipt.merchant)
        for transaction in transactions:
            lag = (transaction.date - receipt.date).days
            if (receipt.amount > 0 and transaction.amount < 0
                    and receipt.currency == transaction.currency
                    and key[1] and key == merchant_key(transaction.merchant)
                    and 0 <= lag <= MAX_SETTLEMENT_DAYS):
                candidates[receipt.id].append(transaction)
                competitors[transaction.id].append(receipt.id)

    outcomes = {}
    for receipt in receipts:
        if receipt.id in accepted:
            continue  # Accepted matches are final for this milestone.
        possible = sorted(candidates[receipt.id], key=lambda item: str(item.id))
        reasons = []
        if not possible:
            reasons.append("unsupported_receipt_amount" if receipt.amount <= 0 else "no_plausible_candidates")
        else:
            if len(possible) > 1:
                reasons.append("multiple_transactions_for_receipt")
            if any(len(competitors[item.id]) > 1 for item in possible):
                reasons.append("multiple_receipts_for_transaction")
            if any(item.amount.copy_negate() != receipt.amount for item in possible):
                reasons.append("amount_difference")
            if any(item.id in used_transactions for item in possible):
                reasons.append("transaction_already_matched")
        clear = bool(possible) and not reasons
        details = {
            "rules_version": RULES_VERSION,
            "reasons": reasons or ["clear_unique_exact"],
            "receipt": {"merchant": receipt.merchant, "date": receipt.date.isoformat(),
                        "amount": format(receipt.amount, ".2f"), "currency": receipt.currency},
            "candidates": [
                {"bank_transaction_id": str(item.id), "merchant": item.merchant,
                 "date": item.date.isoformat(), "amount": format(item.amount, ".2f"),
                 "currency": item.currency, "date_lag_days": (item.date - receipt.date).days,
                 "amount_difference": format(item.amount.copy_negate() - receipt.amount, ".2f"),
                 "competing_receipt_ids": sorted(str(rid) for rid in competitors[item.id] if rid != receipt.id)}
                for item in possible
            ],
        }
        outcomes[receipt.id] = Outcome(
            "matched" if clear else "needs_review" if possible else "unmatched",
            possible[0].id if clear else None, details,
        )
    return outcomes


def assign_receipts_to_account(engine: Engine, account_id: UUID, receipt_ids: list[UUID]) -> int:
    """Persist explicit scope membership once, with one receipt.scoped event.

    A receipt can belong to one account only; reassignment is deferred. Account
    and receipt row locks serialize assignment with reconciliation and each other.
    """
    if not isinstance(account_id, UUID) or any(not isinstance(rid, UUID) for rid in receipt_ids):
        raise ValueError("account_id and receipt_ids must be UUIDs")
    if engine.dialect.name != "postgresql":
        raise ValueError("receipt scope assignment requires PostgreSQL")
    receipt_ids = sorted(set(receipt_ids))
    assigned = 0
    with engine.begin() as connection:
        if connection.scalar(select(Account.id).where(Account.id == account_id).with_for_update()) is None:
            raise ValueError("account_id does not identify an existing account")
        rows = connection.execute(select(Receipt.id, Receipt.account_id).where(
            Receipt.id.in_(receipt_ids)).order_by(Receipt.id).with_for_update()).all()
        if len(rows) != len(receipt_ids):
            raise ValueError("one or more receipt_ids do not identify existing receipts")
        if any(row.account_id not in (None, account_id) for row in rows):
            raise ValueError("a receipt is already assigned to another account")
        for row in rows:
            if row.account_id is not None:
                continue
            connection.execute(update(Receipt).where(Receipt.id == row.id).values(
                account_id=account_id, updated_at=func.now()))
            connection.execute(insert(AuditEvent).values(
                receipt_id=row.id, event_type="receipt.scoped", payload={"account_id": str(account_id)}))
            assigned += 1
    return assigned


@dataclass(frozen=True)
class ReconciliationSummary:
    matched: int
    needs_review: int
    unmatched: int
    unmatched_transactions: int
    decisions_written: int


def reconcile_account(engine: Engine, account_id: UUID) -> ReconciliationSummary:
    """Reconcile all receipts assigned to this account in one transaction.

    READ COMMITTED is required. Account row locking serializes concurrent runs
    and scope assignment. New bank imports after the bank snapshot participate
    on the next run; accepted decisions are not automatically reopened.
    """
    if not isinstance(account_id, UUID):
        raise ValueError("account_id must be a UUID")
    if engine.dialect.name != "postgresql":
        raise ValueError("reconciliation requires PostgreSQL")
    with engine.begin() as connection:
        if connection.scalar(select(Account.id).where(Account.id == account_id).with_for_update()) is None:
            raise ValueError("account_id does not identify an existing account")
        receipts = [Record(row.id, row.merchant, row.receipt_date, row.amount, row.currency)
                    for row in connection.execute(select(Receipt.__table__).where(
                        Receipt.account_id == account_id).order_by(Receipt.id))]
        transactions = [Record(row.id, row.merchant, row.transaction_date, row.amount, row.currency)
                        for row in connection.execute(select(BankTransaction.__table__).where(
                            BankTransaction.account_id == account_id).order_by(BankTransaction.id))]
        previous = {row.receipt_id: row for row in connection.execute(
            select(ReconciliationDecision.__table__).where(ReconciliationDecision.account_id == account_id))}
        accepted = {rid: row.bank_transaction_id for rid, row in previous.items() if row.status == "matched"}
        outcomes = decide(receipts, transactions, accepted)
        written = 0
        for rid, outcome in outcomes.items():
            old = previous.get(rid)
            if old and old.status == outcome.status and old.details == outcome.details:
                continue
            revision = old.revision + 1 if old else 1
            values = {"status": outcome.status, "bank_transaction_id": outcome.bank_transaction_id,
                      "details": outcome.details, "revision": revision}
            if old:
                connection.execute(update(ReconciliationDecision).where(
                    ReconciliationDecision.id == old.id).values(**values, updated_at=func.now()))
            else:
                connection.execute(insert(ReconciliationDecision).values(
                    **values, receipt_id=rid, account_id=account_id))
            connection.execute(insert(AuditEvent).values(
                receipt_id=rid, event_type="receipt.reconciliation_decided",
                payload={"account_id": str(account_id), "revision": revision,
                         "status": outcome.status,
                         "bank_transaction_id": str(outcome.bank_transaction_id) if outcome.bank_transaction_id else None,
                         **outcome.details}))
            written += 1
        statuses = ["matched"] * len(accepted) + [outcome.status for outcome in outcomes.values()]
        summary = ReconciliationSummary(
            matched=statuses.count("matched"), needs_review=statuses.count("needs_review"),
            unmatched=statuses.count("unmatched"),
            unmatched_transactions=len(transactions) - statuses.count("matched"), decisions_written=written,
        )
    return summary
