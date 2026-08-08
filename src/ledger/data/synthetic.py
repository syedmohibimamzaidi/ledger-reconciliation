"""Synthetic receipt corpus.

This exists so `make eval` works on a fresh clone with no dataset download, and
so CI has something deterministic to run. It is NOT a substitute for SROIE.

Never report a headline extraction number from this generator: the receipts are
drawn from the same templates the regex baseline was written against, so any
score here is self-graded. Its only jobs are (a) smoke-testing the pipeline and
(b) giving the eval harness a shape to exercise.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from ..schema import Receipt, LineItem

VENDORS = [
    "BLUE BOTTLE COFFEE", "LOBLAWS", "SHOPPERS DRUG MART", "TIM HORTONS",
    "UBER CANADA", "PRESOTEA", "METRO INC", "CANADIAN TIRE", "SOBEYS",
    "A&W RESTAURANTS", "STAPLES CANADA", "PHARMAPRIX", "IKEA NORTH YORK",
    "FRESHCO", "PETRO CANADA", "THE KEG STEAKHOUSE", "INDIGO BOOKS",
    "REAL FRUIT BUBBLE TEA", "NO FRILLS", "SECOND CUP COFFEE",
]

ITEMS = [
    ("LATTE 12OZ", 4.75), ("CROISSANT", 3.50), ("MILK 2L", 5.49),
    ("BANANAS 1KG", 1.99), ("SHAMPOO 400ML", 8.99), ("NOTEBOOK A5", 6.25),
    ("USB C CABLE", 14.99), ("SANDWICH", 9.75), ("ICED TEA", 3.25),
    ("BATTERIES AA4", 7.49), ("PASTA 500G", 2.79), ("OLIVE OIL 1L", 12.99),
]

_TEMPLATES = [
    "{v}\n{addr}\nTEL: {tel}\n\n{lines}\n\nSUBTOTAL {sub}\nTAX {tax}\nTOTAL {tot}\n\nDATE: {d}\nTHANK YOU",
    "*** {v} ***\n{addr}\n{d}\n\n{lines}\n--------------------\nTOTAL: ${tot}\nVISA ************1234\nAPPROVED",
    "{v}\nINVOICE\nDate {d}\n\n{lines}\n\nAmount Due  {tot}\nGST INCLUDED\n{addr}",
]


def _fmt_date(d: date, rng: random.Random) -> str:
    return rng.choice([d.strftime("%d/%m/%Y"), d.strftime("%d-%m-%Y"),
                       d.strftime("%d %b %Y").upper(), d.strftime("%Y-%m-%d")])


def generate(n: int = 400, seed: int = 7) -> list[Receipt]:
    rng = random.Random(seed)
    start = date(2025, 1, 1)
    out: list[Receipt] = []

    for i in range(n):
        vendor = rng.choice(VENDORS)
        d = start + timedelta(days=rng.randint(0, 540))
        items = rng.sample(ITEMS, k=rng.randint(1, 5))
        lis, sub = [], 0.0
        for name, price in items:
            qty = rng.randint(1, 3)
            line_total = round(price * qty, 2)
            sub += line_total
            lis.append(LineItem(name=name, qty=qty, unit_price=price, price=line_total))
        sub = round(sub, 2)
        tax = round(sub * 0.13, 2)
        tot = round(sub + tax, 2)

        body = "\n".join(
            f"{li.name:<22}{li.qty} x {li.unit_price:>7.2f}{li.price:>10.2f}" for li in lis
        )
        text = rng.choice(_TEMPLATES).format(
            v=vendor,
            addr=f"{rng.randint(1, 999)} {rng.choice(['KING','QUEEN','YONGE','BLOOR'])} ST W",
            tel=f"{rng.randint(200,999)}-{rng.randint(200,999)}-{rng.randint(1000,9999)}",
            lines=body, sub=f"{sub:.2f}", tax=f"{tax:.2f}", tot=f"{tot:.2f}",
            d=_fmt_date(d, rng),
        )

        out.append(Receipt(
            receipt_id=f"SYN{i:05d}", raw_text=text, vendor=vendor, date=d,
            total=tot, line_items=lis, source="synthetic",
        ))
    return out
