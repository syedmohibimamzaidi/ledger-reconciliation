"""Metrics.

Extraction is scored two ways on purpose:

  STRICT     - exact string equality, the official SROIE protocol. Comparable
               to published leaderboard numbers.
  NORMALISED - case/whitespace/punctuation folded, dates as dates, amounts to
               2dp. Closer to what the downstream matcher actually needs.

Report both. Reporting only the normalised score looks like inflating; only the
strict score understates a system that is fine for its actual purpose. The gap
between them is itself informative.

Note the precision/recall distinction: a field left NULL is an abstention, not
an error. A system that declines to guess when unsure has lower recall and
higher precision, and for a human-review workflow that is often the right
trade. Accuracy alone would hide this.
"""

from __future__ import annotations

import math
import re
from datetime import date

from ..schema import Receipt, MatchPair, HEADER_FIELDS


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _norm_str(s) -> str:
    if s is None:
        return ""
    return re.sub(r"[^A-Z0-9 ]", " ", str(s).upper()).strip()


def _norm(field: str, v, strict: bool):
    if v is None:
        return None
    if strict:
        return str(v)
    if field == "total":
        return round(float(v), 2)
    if field == "date":
        return v.isoformat() if isinstance(v, date) else str(v)
    return re.sub(r"\s+", " ", _norm_str(v))


def field_metrics(
    preds: list[Receipt], golds: list[Receipt], strict: bool = False
) -> dict:
    gold_by_id = {r.receipt_id: r for r in golds}
    out: dict[str, dict] = {}

    for f in HEADER_FIELDS:
        tp = fp = fn = 0
        for p in preds:
            g = gold_by_id.get(p.receipt_id)
            if g is None:
                continue
            pv, gv = _norm(f, getattr(p, f), strict), _norm(f, getattr(g, f), strict)
            if gv is None:
                continue  # no ground truth for this field; excluded, not counted
            if pv is None:
                fn += 1
            elif pv == gv:
                tp += 1
            else:
                fp += 1
                fn += 1
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[f] = {"precision": round(prec, 4), "recall": round(rec, 4),
                  "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn,
                  "support": tp + fn}

    sup = sum(v["support"] for v in out.values()) or 1
    macro = sum(v["f1"] for v in out.values()) / len(HEADER_FIELDS)
    micro = sum(v["f1"] * v["support"] for v in out.values()) / sup
    out["macro_f1"] = round(macro, 4)
    out["micro_f1"] = round(micro, 4)
    return out


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def matching_metrics(pred: list[MatchPair], gold: list[MatchPair]) -> dict:
    p = {x.key() for x in pred}
    g = {x.key() for x in gold}
    tp, fp, fn = len(p & g), len(p - g), len(g - p)

    # A wrong-partner error is worse than an abstention: it silently books a
    # receipt against the wrong charge. Track it separately.
    gold_r = {r for r, _ in g}
    wrong_partner = len({(r, t) for r, t in (p - g) if r in gold_r})

    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
        "tp": tp, "fp": fp, "fn": fn,
        "wrong_partner": wrong_partner,
        "spurious_on_orphan": len(p - g) - wrong_partner,
        "automation_rate": round(len(p) / max(len(gold), 1), 4),
    }


def blocking_recall(
    receipts, txns, gold: list[MatchPair], candidates_fn
) -> dict:
    """Ceiling check. Every point lost here is unreachable downstream."""
    gold_map = {r: t for r, t in (x.key() for x in gold)}
    hit = total = 0
    for r in receipts:
        want = gold_map.get(r.receipt_id)
        if want is None:
            continue
        total += 1
        if any(t.txn_id == want for t in candidates_fn(r, txns)):
            hit += 1
    return {"blocking_recall": round(hit / total, 4) if total else 0.0,
            "recoverable_pairs": hit, "gold_pairs": total}


# --------------------------------------------------------------------------
# calibration (used from Phase 2, defined now so thresholds are never guessed)
# --------------------------------------------------------------------------

def reliability(pred: list[MatchPair], gold: list[MatchPair], bins: int = 10) -> dict:
    g = {x.key() for x in gold}
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for x in pred:
        i = min(int(x.confidence * bins), bins - 1)
        buckets[i].append((x.confidence, 1 if x.key() in g else 0))

    curve, ece, brier, n = [], 0.0, 0.0, len(pred) or 1
    for i, b in enumerate(buckets):
        if not b:
            continue
        conf = sum(c for c, _ in b) / len(b)
        acc = sum(y for _, y in b) / len(b)
        curve.append({"bin": f"{i/bins:.1f}-{(i+1)/bins:.1f}", "n": len(b),
                      "mean_confidence": round(conf, 4), "accuracy": round(acc, 4)})
        ece += (len(b) / n) * abs(acc - conf)
    for x in pred:
        brier += (x.confidence - (1 if x.key() in g else 0)) ** 2
    return {"curve": curve, "ece": round(ece, 4), "brier": round(brier / n, 4)}


def threshold_sweep(pred: list[MatchPair], gold: list[MatchPair],
                    steps: int = 11) -> list[dict]:
    """The automation-rate / precision tradeoff curve. This is the plot that
    justifies where the review-queue cutoff goes."""
    rows = []
    for i in range(steps):
        t = i / (steps - 1)
        kept = [p for p in pred if p.confidence >= t]
        m = matching_metrics(kept, gold)
        rows.append({"threshold": round(t, 2), "precision": m["precision"],
                     "recall": m["recall"], "auto_cleared": len(kept),
                     "sent_to_review": len(pred) - len(kept)})
    return rows
