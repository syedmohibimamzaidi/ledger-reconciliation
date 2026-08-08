"""`python -m ledger.eval.run_eval` - the one command that says whether anything works.

Writes evals/<timestamp>__<corpus>__<split>.json and commits-worthy summaries.
Every run records the corruption model version and the git commit, so a number
in the README can always be traced back to the code and difficulty that made it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..data import splits as splits_mod
from ..data import synthetic
from ..data.corruption import DEFAULT, EASY
from ..data.ledger_gen import build_ledger
from ..baseline import extract as base_extract
from ..baseline import match as base_match
from ..schema import to_jsonable
from . import metrics


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "nogit"


def load_corpus(name: str):
    if name == "synthetic":
        return synthetic.generate()
    if name == "sroie":
        from ..data import sroie
        return sroie.load()
    raise ValueError(f"unknown corpus: {name}")


def run(corpus: str, split_name: str, difficulty: str, threshold: float) -> dict:
    receipts = load_corpus(corpus)
    split = splits_mod.load_or_create(receipts, f"{corpus}-v1")
    if split_name != "all":
        receipts = splits_mod.subset(receipts, split[split_name])

    cfg = EASY if difficulty == "easy" else DEFAULT
    ds = build_ledger(receipts, cfg)

    preds = base_extract.extract_all(receipts)
    ext_norm = metrics.field_metrics(preds, receipts, strict=False)
    ext_strict = metrics.field_metrics(preds, receipts, strict=True)

    # Matching is fed PREDICTED headers, not gold. Feeding it gold receipts
    # measures a system that does not exist; errors compound and that is the
    # number that matters.
    pairs, mstats = base_match.match_all(preds, ds.transactions, threshold=threshold)
    match = metrics.matching_metrics(pairs, ds.gold_pairs)
    block = metrics.blocking_recall(
        preds, ds.transactions, ds.gold_pairs, base_match.candidates
    )

    # Oracle: perfect extraction, same matcher. The gap is how much extraction
    # error costs downstream - a number worth quoting in interviews.
    oracle_pairs, _ = base_match.match_all(receipts, ds.transactions, threshold=threshold)
    oracle = metrics.matching_metrics(oracle_pairs, ds.gold_pairs)

    return {
        "run": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git": _git_sha(),
            "corpus": corpus,
            "split": split_name,
            "difficulty": difficulty,
            "system": "baseline-rules",
        },
        "dataset": to_jsonable(ds.summary()),
        "extraction": {"normalised": ext_norm, "strict": ext_strict},
        "matching": {
            **match,
            **block,
            "blocking": mstats,
            "oracle_extraction_f1": oracle["f1"],
            "cost_of_extraction_error_f1": round(oracle["f1"] - match["f1"], 4),
        },
        "calibration": metrics.reliability(pairs, ds.gold_pairs),
        "threshold_sweep": metrics.threshold_sweep(pairs, ds.gold_pairs),
    }


def _print(res: dict) -> None:
    r, d = res["run"], res["dataset"]
    print(f"\n  {r['corpus']}/{r['split']}  difficulty={r['difficulty']}  "
          f"system={r['system']}  git={r['git']}")
    print(f"  {d['n_receipts']} receipts, {d['n_transactions']} txns, "
          f"{d['n_gold_pairs']} gold pairs "
          f"({d['orphan_receipts']} orphan receipts, "
          f"{d['orphan_transactions']} orphan txns)\n")

    print("  EXTRACTION (normalised | strict)")
    n, s = res["extraction"]["normalised"], res["extraction"]["strict"]
    print(f"  {'field':<10}{'P':>8}{'R':>8}{'F1':>8}{'strictF1':>11}{'support':>9}")
    for f in ("vendor", "date", "total"):
        print(f"  {f:<10}{n[f]['precision']:>8.3f}{n[f]['recall']:>8.3f}"
              f"{n[f]['f1']:>8.3f}{s[f]['f1']:>11.3f}{n[f]['support']:>9}")
    print(f"  {'macro':<10}{'':>8}{'':>8}{n['macro_f1']:>8.3f}{s['macro_f1']:>11.3f}\n")

    m = res["matching"]
    print("  MATCHING")
    print(f"    blocking recall     {m['blocking_recall']:.3f}   "
          f"(ceiling; {m['recoverable_pairs']}/{m['gold_pairs']} pairs reachable)")
    print(f"    candidates/receipt  {m['blocking']['candidates_per_receipt']}")
    print(f"    precision           {m['precision']:.3f}")
    print(f"    recall              {m['recall']:.3f}")
    print(f"    F1                  {m['f1']:.3f}")
    print(f"    wrong partner       {m['wrong_partner']}   "
          f"(booked to the wrong charge - the expensive error)")
    print(f"    spurious on orphan  {m['spurious_on_orphan']}")
    print(f"    oracle-extract F1   {m['oracle_extraction_f1']:.3f}   "
          f"(cost of extraction error: {m['cost_of_extraction_error_f1']:+.3f})")
    print(f"\n  CALIBRATION  ECE={res['calibration']['ece']:.3f}  "
          f"Brier={res['calibration']['brier']:.3f}")
    print("    (uninformative until Phase 2 - the baseline emits a blended "
          "heuristic, not a probability)\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="synthetic", choices=["synthetic", "sroie"])
    ap.add_argument("--split", default="dev", choices=["train", "dev", "test", "all"])
    ap.add_argument("--difficulty", default="default", choices=["default", "easy"])
    ap.add_argument("--threshold", type=float, default=0.60)
    ap.add_argument("--out", default="evals")
    args = ap.parse_args()

    if args.split == "test":
        print("\n  *** opening the TEST split ***\n"
              "  Only do this at the end of a phase, and commit whatever it says.\n")

    res = run(args.corpus, args.split, args.difficulty, args.threshold)
    _print(res)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    stamp = res["run"]["timestamp"].replace(":", "").replace("-", "")
    path = Path(args.out) / f"{stamp}__{args.corpus}__{args.split}__{args.difficulty}.json"
    path.write_text(json.dumps(res, indent=2))
    print(f"  wrote {path}\n")


if __name__ == "__main__":
    main()
