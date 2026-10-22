from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from ledger.persistence.reconcile import Record, decide, merchant_key


def receipt(**changes):
    return replace(Record(uuid4(), "Blue Bottle", date(2026, 10, 22), Decimal("12.30"), "CAD"), **changes)


def debit(**changes):
    return receipt(amount=Decimal("-12.30"), **changes)


def test_clear_pair_uses_exact_signed_decimal():
    r, t = receipt(amount=Decimal("0.30")), receipt(amount=Decimal("-0.30"))
    result = decide([r], [t])[r.id]
    assert result.status == "matched" and result.bank_transaction_id == t.id
    assert result.details["reasons"] == ["clear_unique_exact"]


@pytest.mark.parametrize("amount", ["-12.31", "-0.01", "-999.00"])
def test_amount_difference_is_not_blocked_out(amount):
    r, t = receipt(), receipt(amount=Decimal(amount))
    result = decide([r], [t])[r.id]
    assert result.status == "needs_review"
    assert result.details["reasons"] == ["amount_difference"]
    assert len(result.details["candidates"]) == 1


def test_exact_amount_does_not_hide_a_different_amount_competitor():
    r, t1, t2 = receipt(), debit(), receipt(amount=Decimal("-100"))
    result = decide([r], [t1, t2])[r.id]
    assert result.status == "needs_review" and result.bank_transaction_id is None
    assert result.details["reasons"] == ["multiple_transactions_for_receipt", "amount_difference"]


def test_ambiguity_in_both_directions():
    r1, r2, t1, t2 = receipt(), receipt(), debit(), debit()
    for result in decide([r1, r2], [t1, t2]).values():
        assert result.status == "needs_review"
        assert result.details["reasons"] == ["multiple_transactions_for_receipt", "multiple_receipts_for_transaction"]


@pytest.mark.parametrize("lag,status", [(-1, "unmatched"), (0, "matched"), (4, "matched"), (5, "unmatched")])
def test_settlement_date_boundaries(lag, status):
    r, t = receipt(), debit(date=date(2026, 10, 22) + timedelta(days=lag))
    assert decide([r], [t])[r.id].status == status


@pytest.mark.parametrize("changes", [
    {"currency": "USD"}, {"amount": Decimal("12.30")}, {"amount": Decimal("0")},
    {"merchant": "Other Store"},
])
def test_currency_direction_and_merchant_exclusions(changes):
    r, t = receipt(), replace(debit(), **changes)
    result = decide([r], [t])[r.id]
    assert result.status == "unmatched" and not result.details["candidates"]


def test_zero_receipt_is_not_auto_matched():
    r = receipt(amount=Decimal("0"))
    assert decide([r], [debit()])[r.id].details["reasons"] == ["unsupported_receipt_amount"]


def test_merchant_guard_preserves_meaningful_tokens():
    assert merchant_key(" SQ *Blue-Bottle ") == merchant_key("blue bottle")
    assert merchant_key("Store #123") != merchant_key("Store #456")
    assert merchant_key("Blue Bottle Toronto") != merchant_key("Blue Bottle Vancouver")
    assert merchant_key("Cafe Inc") != merchant_key("Cafe")
    assert merchant_key("Café") != merchant_key("Cafe")


def test_previously_accepted_receipts_remain_competitors():
    r1, r2, t1, t2 = receipt(), receipt(), debit(), debit()
    result = decide([r1, r2], [t1, t2], {r1.id: t1.id})
    assert r1.id not in result
    assert result[r2.id].status == "needs_review"
    assert "transaction_already_matched" in result[r2.id].details["reasons"]
    assert "multiple_receipts_for_transaction" in result[r2.id].details["reasons"]
