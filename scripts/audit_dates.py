"""Which gold date annotations does our parser fail on?

field_metrics skips any field where gold is None, so a date format the parser
can't read doesn't count as an error - it silently leaves the denominator.
That inflates the score on whatever remains. This script finds those cases.
"""

import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, "src")
from ledger.data.sroie import parse_date  # noqa: E402

bad = []
total = 0
for f in sorted(pathlib.Path("data/sroie/entities").glob("*.txt")):
    ann = json.loads(f.read_text())
    raw = ann.get("date")
    if not raw:
        continue
    total += 1
    if parse_date(raw) is None:
        bad.append((f.stem, raw))

print(f"{len(bad)} unparseable of {total} annotated dates\n")
for rid, raw in bad[:40]:
    print(f"  {rid:<24} {raw!r}")

if bad:
    print("\nshapes:")
    for shape, n in Counter(
        "".join("9" if c.isdigit() else "A" if c.isalpha() else c for c in raw)
        for _, raw in bad
    ).most_common(15):
        print(f"  {n:>4}  {shape}")