"""The corruption model.

READ THIS BEFORE TRUSTING ANY MATCHING NUMBER IN THIS REPO.

No public dataset pairs receipts with the bank transactions they settled to, so
the transaction ledger here is generated from receipt ground truth and then
degraded. That means matching metrics are measured *under a stated corruption
model*, not against real bank data. Every parameter below is a claim about how
messy the real world is, and every one of them is arguable.

The point of putting them in one versioned file is that a reader can disagree
with a number, change it, and re-run. A matching score is only meaningful if
you can see the difficulty knobs that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

# Bump when any default below changes, so committed eval results stay traceable.
CORRUPTION_MODEL_VERSION = "0.1.0"

# Real acquirer/processor prefixes that appear in card descriptors.
ACQUIRER_PREFIXES = ("SQ *", "TST* ", "PP*", "SP ", "PY *", "IC* ", "")
CITY_SUFFIXES = (
    " TORONTO ON", " MISSISSAUGA ON", " VANCOUVER BC", " MONTREAL QC",
    " 800-123-4567", " WWW.EXAMPLE.CA", "",
)


@dataclass
class CorruptionConfig:
    seed: int = 20260809

    # --- Settlement lag: receipt date -> posted date, in days ---
    # Weekend/holiday batching is why 2-3 day lags are common, not rare.
    lag_days_weights: dict[int, float] = field(
        default_factory=lambda: {0: 0.30, 1: 0.40, 2: 0.20, 3: 0.10}
    )

    # --- Amount drift ---
    # Fraction of receipts that are restaurant-style and get a tip added.
    tip_probability: float = 0.22
    tip_pct_choices: tuple[float, ...] = (0.10, 0.12, 0.15, 0.18, 0.20)
    # Partial refund / price adjustment after the receipt printed.
    partial_refund_probability: float = 0.03
    partial_refund_pct_range: tuple[float, float] = (0.05, 0.35)
    # Rounding noise from currency conversion or cash-back.
    cent_jitter_probability: float = 0.08

    # --- Descriptor mangling ---
    truncate_probability: float = 0.45
    truncate_max_chars: int = 22
    store_number_probability: float = 0.35
    acquirer_prefix_probability: float = 0.40
    city_suffix_probability: float = 0.50
    vowel_strip_probability: float = 0.08  # aggressive legacy POS systems

    # --- Population effects ---
    # Receipts with no transaction at all (paid cash, or feed gap).
    orphan_receipt_rate: float = 0.08
    # Extra transactions with no receipt, as a multiple of receipt count.
    orphan_txn_multiplier: float = 0.25
    # Duplicate charges (double-tap, pre-auth + capture).
    duplicate_txn_rate: float = 0.02
    # Hard negatives: same vendor, near-identical amount, within a few days.
    # This is the knob that actually makes matching non-trivial. Turn it to 0
    # and precision will look great and mean nothing.
    confuser_rate: float = 0.18
    confuser_amount_jitter: float = 0.04
    confuser_date_window: int = 4

    def as_dict(self) -> dict:
        d = asdict(self)
        d["model_version"] = CORRUPTION_MODEL_VERSION
        return d


DEFAULT = CorruptionConfig()

# A deliberately easy config, for sanity-checking that the pipeline can score
# well when the problem IS easy. If the baseline can't near-saturate this, the
# bug is in the code, not the model.
EASY = CorruptionConfig(
    tip_probability=0.0,
    partial_refund_probability=0.0,
    cent_jitter_probability=0.0,
    truncate_probability=0.0,
    store_number_probability=0.0,
    acquirer_prefix_probability=0.0,
    city_suffix_probability=0.0,
    vowel_strip_probability=0.0,
    orphan_receipt_rate=0.0,
    orphan_txn_multiplier=0.0,
    duplicate_txn_rate=0.0,
    confuser_rate=0.0,
    lag_days_weights={0: 1.0},
)
