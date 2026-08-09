"""Tests for the things that fail silently.

None of these check "is the score good". They check that the measurement
instrument is not lying - leakage, non-determinism, and gold-label bugs are the
failures that produce a confident wrong number, which is worse than a low one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ledger.data import synthetic, splits
from ledger.data.corruption import CorruptionConfig, EASY
from ledger.data.ledger_gen import build_ledger, mangle_descriptor
from ledger.baseline import extract as bx, match as bm
from ledger.eval import metrics


def test_generator_is_deterministic():
    a, b = synthetic.generate(50, seed=1), synthetic.generate(50, seed=1)
    assert [r.total for r in a] == [r.total for r in b]
    assert [r.vendor for r in a] == [r.vendor for r in b]


def test_ledger_is_deterministic():
    rs = synthetic.generate(60)
    l1, l2 = build_ledger(rs), build_ledger(rs)
    assert [t.txn_id for t in l1.transactions] == [t.txn_id for t in l2.transactions]
    assert [t.amount for t in l1.transactions] == [t.amount for t in l2.transactions]


def test_gold_pairs_are_one_to_one():
    ds = build_ledger(synthetic.generate(120))
    rids = [p.receipt_id for p in ds.gold_pairs]
    tids = [p.txn_id for p in ds.gold_pairs]
    assert len(rids) == len(set(rids)), "a receipt is paired twice"
    assert len(tids) == len(set(tids)), "a transaction is paired twice"


def test_gold_pairs_point_at_real_objects():
    ds = build_ledger(synthetic.generate(80))
    rids = {r.receipt_id for r in ds.receipts}
    tids = {t.txn_id for t in ds.transactions}
    for p in ds.gold_pairs:
        assert p.receipt_id in rids and p.txn_id in tids


def test_duplicates_and_confusers_are_not_in_gold():
    """A duplicate charge is a real second transaction with no receipt behind
    it. If it were in gold, a matcher would be rewarded for booking the same
    receipt twice - the exact bug this project exists to prevent."""
    ds = build_ledger(synthetic.generate(200))
    paired = {p.txn_id for p in ds.gold_pairs}
    for t in ds.transactions:
        if t._origin_receipt_id is None:
            assert t.txn_id not in paired


def test_easy_config_is_actually_easy():
    """Sanity floor. If the baseline cannot near-saturate a corpus with zero
    corruption, the bug is in the code, not the model."""
    rs = synthetic.generate(150)
    ds = build_ledger(rs, EASY)
    pairs, _ = bm.match_all(rs, ds.transactions)
    m = metrics.matching_metrics(pairs, ds.gold_pairs)
    assert m["precision"] > 0.95 and m["recall"] > 0.90, m


def test_extractor_never_sees_ground_truth():
    """Blank the labels; predictions must be unchanged. Catches the classic
    accidental-leak where a field is copied rather than parsed."""
    rs = synthetic.generate(40)
    before = bx.extract_all(rs)
    for r in rs:
        r.vendor, r.date, r.total = None, None, None
    after = bx.extract_all(rs)
    assert [(x.vendor, x.date, x.total) for x in before] == \
           [(x.vendor, x.date, x.total) for x in after]


def test_split_is_grouped_by_vendor():
    """No vendor may appear in two splits, or templates leak across the wall."""
    rs = synthetic.generate(300)
    sp = splits.make(rs, "test-split")
    by_id = {r.receipt_id: r for r in rs}
    seen = {}
    for part, ids in sp.items():
        for i in ids:
            v = splits._vendor_key(by_id[i])
            assert seen.setdefault(v, part) == part, f"vendor {v} spans splits"


def test_split_covers_everything_exactly_once():
    rs = synthetic.generate(120)
    sp = splits.make(rs, "cover")
    ids = [i for part in sp.values() for i in part]
    assert sorted(ids) == sorted(r.receipt_id for r in rs)
    assert len(ids) == len(set(ids))


def test_normalisation_never_scores_below_strict():
    rs = synthetic.generate(100)
    preds = bx.extract_all(rs)
    n = metrics.field_metrics(preds, rs, strict=False)
    s = metrics.field_metrics(preds, rs, strict=True)
    assert n["macro_f1"] >= s["macro_f1"] - 1e-9


def test_matching_metrics_arithmetic():
    from ledger.schema import MatchPair
    gold = [MatchPair("r1", "t1"), MatchPair("r2", "t2"), MatchPair("r3", "t3")]
    pred = [MatchPair("r1", "t1", 0.9), MatchPair("r2", "t9", 0.8)]
    m = metrics.matching_metrics(pred, gold)
    assert (m["tp"], m["fp"], m["fn"]) == (1, 1, 2)
    assert m["wrong_partner"] == 1
    assert abs(m["precision"] - 0.5) < 1e-9


def test_descriptor_mangling_preserves_some_signal():
    """If mangling destroyed all merchant identity the task would be
    impossible, and a low score would mean nothing."""
    import random
    rng = random.Random(0)
    cfg = CorruptionConfig()
    hits = sum(
        bm.normalize_merchant(mangle_descriptor("BLUE BOTTLE COFFEE", rng, cfg))
        .startswith("BLUE")
        for _ in range(200)
    )
    assert hits > 100, f"only {hits}/200 descriptors kept the leading token"

def test_date_parsing_covers_observed_sroie_formats():
    from datetime import date as D
    from ledger.data.sroie import parse_date
    cases = {
        "27/MAR/2018": D(2018, 3, 27), "22 MAR 18": D(2018, 3, 22),
        "28-FEB-2018": D(2018, 2, 28), "2018/03/27": D(2018, 3, 27),
        "20180304": D(2018, 3, 4),     "25032018": D(2018, 3, 25),
        "23.03.18": D(2018, 3, 23),    "(06/12/2016)": D(2016, 12, 6),
        "OCT 3, 2016": D(2016, 10, 3), "12/28/2017": D(2017, 12, 28),
        "4/22/2018": D(2018, 4, 22),
    }
    for raw, want in cases.items():
        assert parse_date(raw) == want, f"{raw!r} -> {parse_date(raw)}"


def test_ambiguous_dates_resolve_day_first():
    """Malaysian corpus: 05/06/2017 is 5 June, not 6 May. Day-first formats
    are tried first so this can't flip silently."""
    from datetime import date as D
    from ledger.data.sroie import parse_date
    assert parse_date("05/06/2017") == D(2017, 6, 5)