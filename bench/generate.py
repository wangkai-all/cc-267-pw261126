"""Small benchmark fixture generator.

Generates nested orders with a controlled number of items/discounts/fulfillments
so the naive three-way expansion is visibly explosive.  Output is written to
stdout as JSON; no randomness, so repeated runs are byte-identical.
"""

from __future__ import annotations

import argparse
import json
import sys


def make_order(n: int, items: int, discounts: int, fulfillments: int) -> dict:
    order_id = f"BENCH-{n:04d}"
    item_rows = [
        {
            "item_id": f"{order_id}-I{i}",
            "sku": f"SKU-{i % 7}",
            "title": f"商品 {i}",
            "qty": (i % 3) + 1,
            "unit_price_cents": 1000,
        }
        for i in range(items)
    ]
    return {
        "schema_version": "orders.v3",
        "order_id": order_id,
        "tenant": "bench",
        "channel": "job" if n % 3 else "web",
        "status": "paid",
        "placed_at": f"2026-10-{(n % 28) + 1:02d}T12:00:00Z",
        "currency": "CNY",
        "order_amount": sum(r["qty"] * r["unit_price_cents"] for r in item_rows),
        "items": item_rows,
        "discounts": [
            {
                "discount_id": f"{order_id}-D{i}",
                "code": f"CODE-{i % 3}",
                "kind": "coupon" if i % 2 else "promotion",
                "amount_cents": 100,
            }
            for i in range(discounts)
        ],
        "fulfillments": [
            {
                "fulfillment_id": f"{order_id}-F{i}",
                "carrier": ["SF", "YTO", "JD"][i % 3],
                "tracking_no": f"T{n}-{i}",
                "shipping_fee_cents": 200,
                "lines": [{"item_id": f"{order_id}-I{j}"} for j in range(min(items, 2))],
            }
            for i in range(fulfillments)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--items", type=int, default=8)
    parser.add_argument("--discounts", type=int, default=4)
    parser.add_argument("--fulfillments", type=int, default=3)
    parser.add_argument("--empty-every", type=int, default=11, help="every N-th order has empty arrays")
    args = parser.parse_args(argv)

    docs = []
    for n in range(args.orders):
        empty = args.empty_every and n % args.empty_every == 0
        docs.append(
            make_order(
                n,
                0 if empty else args.items,
                0 if empty else args.discounts,
                0 if empty else args.fulfillments,
            )
        )
    json.dump(docs, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
