"""Baseline matcher: block on (date window, amount band), score by similarity.

Two things are being measured here and they should not be conflated:

  * BLOCKING RECALL - what fraction of true pairs even survive candidate
    generation. This is a ceiling on everything downstream. If blocking recall
    is 0.94, no scorer can exceed 0.94 recall, and tuning the scorer is wasted
    effort until blocking is fixed.
  * SCORING QUALITY - given candidates, how well do we pick.

Phase 2 replaces the similarity heuristic with a learned pair scorer. Blocking
mostly stays, because it is what keeps the problem sub-quadratic.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..schema import Receipt, Transaction, MatchPair

# Tokens that carry no merchant identity - stripped before comparison.
_STOP = {
    "INC", "LTD", "LLC", "CO", "CORP", "SDN", "BHD", "THE", "CANADA", "ON",
    "BC", "QC", "AB", "TORONTO", "VANCOUVER", "MONTREAL", "MISSISSAUGA",
}
_ACQ = re.compile(r"^(SQ \*|TST\* ?|PP\*|SP |PY \*|IC\* ?)")


def normalize_merchant(s: str) -> str:
    """Cheap normalisation. Phase 2 replaces this with a real entity-resolution
    component that gets its own eval - it is the most interesting ML here."""
    s = _ACQ.sub("", (s or "").upper())
    s = re.sub(r"#\s*\d+", " ", s)
    s = re.sub(r"\b\d{3}[- ]?\d{3}[- ]?\d{4}\b", " ", s)
    s = re.sub(r"WWW\.\S+|\.(COM|CA|NET)\b", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    toks = [t for t in s.split() if t not in _STOP and len(t) > 1]
    return " ".join(toks).strip()


def _sim(a: str, b: str) -> float:
    a, b = normalize_merchant(a), normalize_merchant(b)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Prefix credit: descriptors are frequently truncated, so a short string
    # that prefixes a longer one is strong evidence, not weak.
    if a.startswith(b[:8]) or b.startswith(a[:8]):
        ratio = max(ratio, 0.75)
    return ratio


def candidates(
    receipt: Receipt,
    txns: list[Transaction],
    date_window: int = 4,
    amount_band: float = 0.25,
) -> list[Transaction]:
    """Blocking. Wide enough to survive tips (+20%) and settlement lag."""
    if receipt.date is None or receipt.total is None:
        return []
    lo, hi = receipt.total * (1 - amount_band), receipt.total * (1 + amount_band)
    out = []
    for t in txns:
        if not (lo <= t.amount <= hi):
            continue
        delta = (t.posted_date - receipt.date).days
        if -1 <= delta <= date_window:
            out.append(t)
    return out


def score(receipt: Receipt, txn: Transaction) -> float:
    """Hand-weighted blend. Phase 2 learns these weights instead of asserting
    them, and reports whether learning actually helped."""
    if receipt.total is None or receipt.date is None:
        return 0.0
    amt_err = abs(txn.amount - receipt.total) / max(receipt.total, 1.0)
    amt = max(0.0, 1.0 - amt_err / 0.25)
    days = (txn.posted_date - receipt.date).days
    dt = max(0.0, 1.0 - max(0, days) / 5.0)
    name = _sim(txn.descriptor, receipt.vendor or "")
    return round(0.45 * name + 0.35 * amt + 0.20 * dt, 4)


def match_all(
    receipts: list[Receipt],
    txns: list[Transaction],
    threshold: float = 0.60,
    **block_kw,
) -> tuple[list[MatchPair], dict]:
    """Greedy 1:1 assignment above `threshold`.

    Greedy is deliberate for a baseline. Global assignment (Hungarian) is the
    obvious alternative and is worth trying in Phase 2 - the tradeoff is O(n^3)
    against a tiny accuracy gain, which is exactly the kind of thing to have a
    real answer about in an interview.
    """
    scored: list[tuple[float, str, str]] = []
    n_cand = 0
    for r in receipts:
        cands = candidates(r, txns, **block_kw)
        n_cand += len(cands)
        for t in cands:
            s = score(r, t)
            if s >= threshold:
                scored.append((s, r.receipt_id, t.txn_id))

    scored.sort(reverse=True)
    used_r, used_t, pairs = set(), set(), []
    for s, rid, tid in scored:
        if rid in used_r or tid in used_t:
            continue
        used_r.add(rid)
        used_t.add(tid)
        pairs.append(MatchPair(receipt_id=rid, txn_id=tid, confidence=s))

    stats = {
        "candidates_generated": n_cand,
        "candidates_per_receipt": round(n_cand / max(len(receipts), 1), 2),
        "pairs_before_assignment": len(scored),
        "pairs_after_assignment": len(pairs),
        "threshold": threshold,
    }
    return pairs, stats
