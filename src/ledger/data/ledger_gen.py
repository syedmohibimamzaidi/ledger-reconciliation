"""Derive a transaction ledger + gold pairs from receipt ground truth."""

from __future__ import annotations

import random
import re
from datetime import timedelta

from ..schema import Receipt, Transaction, MatchPair, Dataset
from .corruption import CorruptionConfig, DEFAULT, ACQUIRER_PREFIXES, CITY_SUFFIXES


def _weighted_choice(rng: random.Random, weights: dict) -> int:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def mangle_descriptor(vendor: str, rng: random.Random, cfg: CorruptionConfig) -> str:
    """Turn a clean vendor name into a card-statement descriptor."""
    s = re.sub(r"[^A-Za-z0-9 &]", " ", vendor or "UNKNOWN")
    s = re.sub(r"\s+", " ", s).strip().upper()

    if rng.random() < cfg.vowel_strip_probability and len(s) > 8:
        head, *rest = s.split(" ")
        s = " ".join([head] + [re.sub(r"[AEIOU]", "", w) for w in rest])

    if rng.random() < cfg.truncate_probability:
        s = s[: cfg.truncate_max_chars].strip()

    if rng.random() < cfg.store_number_probability:
        s = f"{s} #{rng.randint(1, 9999):04d}"

    if rng.random() < cfg.acquirer_prefix_probability:
        s = rng.choice(ACQUIRER_PREFIXES) + s

    if rng.random() < cfg.city_suffix_probability:
        s = s + rng.choice(CITY_SUFFIXES)

    return re.sub(r"\s+", " ", s).strip()


def _drift_amount(total: float, rng: random.Random, cfg: CorruptionConfig) -> float:
    amt = total
    if rng.random() < cfg.tip_probability:
        amt *= 1.0 + rng.choice(cfg.tip_pct_choices)
    if rng.random() < cfg.partial_refund_probability:
        lo, hi = cfg.partial_refund_pct_range
        amt *= 1.0 - rng.uniform(lo, hi)
    if rng.random() < cfg.cent_jitter_probability:
        amt += rng.choice([-0.02, -0.01, 0.01, 0.02])
    return round(amt, 2)


def build_ledger(
    receipts: list[Receipt], cfg: CorruptionConfig = DEFAULT
) -> Dataset:
    """Return a Dataset with transactions and gold pairs.

    Gold pairs are 1:1 and cover only receipts that were NOT made orphans.
    Duplicate transactions are intentionally left unpaired in gold: a duplicate
    charge is a real second transaction that the receipt does not justify, and
    a matcher that pairs both should be penalised.
    """
    rng = random.Random(cfg.seed)
    txns: list[Transaction] = []
    pairs: list[MatchPair] = []
    n = 0

    for r in receipts:
        if r.total is None or r.date is None:
            continue  # cannot anchor a transaction without amount + date
        if rng.random() < cfg.orphan_receipt_rate:
            continue

        n += 1
        txn = Transaction(
            txn_id=f"T{n:06d}",
            descriptor=mangle_descriptor(r.vendor or "", rng, cfg),
            posted_date=r.date + timedelta(days=_weighted_choice(rng, cfg.lag_days_weights)),
            amount=_drift_amount(r.total, rng, cfg),
            _origin_receipt_id=r.receipt_id,
        )
        txns.append(txn)
        pairs.append(MatchPair(receipt_id=r.receipt_id, txn_id=txn.txn_id))

        # Hard negative: same merchant, close amount, close date.
        if rng.random() < cfg.confuser_rate:
            n += 1
            j = cfg.confuser_amount_jitter
            txns.append(
                Transaction(
                    txn_id=f"T{n:06d}",
                    descriptor=mangle_descriptor(r.vendor or "", rng, cfg),
                    posted_date=r.date
                    + timedelta(days=rng.randint(-cfg.confuser_date_window,
                                                 cfg.confuser_date_window)),
                    amount=round(r.total * rng.uniform(1 - j, 1 + j), 2),
                    _origin_receipt_id=None,
                )
            )

        if rng.random() < cfg.duplicate_txn_rate:
            n += 1
            txns.append(
                Transaction(
                    txn_id=f"T{n:06d}",
                    descriptor=txn.descriptor,
                    posted_date=txn.posted_date,
                    amount=txn.amount,
                    _origin_receipt_id=None,
                )
            )

    # Unrelated spend the user never kept a receipt for.
    pool = [r for r in receipts if r.total and r.date]
    for _ in range(int(len(pool) * cfg.orphan_txn_multiplier)):
        if not pool:
            break
        src = rng.choice(pool)
        n += 1
        txns.append(
            Transaction(
                txn_id=f"T{n:06d}",
                descriptor=mangle_descriptor(src.vendor or "", rng, cfg),
                posted_date=src.date + timedelta(days=rng.randint(-30, 30)),
                amount=round(src.total * rng.uniform(0.2, 3.0), 2),
                _origin_receipt_id=None,
            )
        )

    rng.shuffle(txns)
    return Dataset(
        receipts=receipts,
        transactions=txns,
        gold_pairs=pairs,
        meta={"corruption": cfg.as_dict()},
    )
