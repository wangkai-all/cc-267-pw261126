"""Benchmark: fan-out cost and strategy comparison.

Runs the real executor over a generated fixture and prints:

* the row count a naive three-array join would produce vs. the rows the
  selected grain actually produces,
* wall-clock time for the aggregate-first path,
* a direct hand-computable check that total item gross across the item grain
  equals SUM(order_amount) across the order grain (they are equal for the
  generated fixture by construction) and that no amount is multiplied by the
  discount/fulfillment counts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.executor import run_query  # noqa: E402
from app.store import Store  # noqa: E402
from bench.generate import make_order  # noqa: E402


def build_docs(orders: int, items: int, discounts: int, fulfillments: int, empty_every: int) -> list[dict]:
    out = []
    for n in range(orders):
        empty = bool(empty_every) and n % empty_every == 0
        out.append(
            make_order(n, 0 if empty else items, 0 if empty else discounts, 0 if empty else fulfillments)
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=300)
    parser.add_argument("--items", type=int, default=8)
    parser.add_argument("--discounts", type=int, default=4)
    parser.add_argument("--fulfillments", type=int, default=3)
    parser.add_argument("--empty-every", type=int, default=11)
    args = parser.parse_args(argv)

    docs = build_docs(args.orders, args.items, args.discounts, args.fulfillments, args.empty_every)
    path = tempfile.mktemp(suffix=".bench.sqlite3")
    store = Store(path)

    t0 = time.perf_counter()
    load = store.load("bench", docs)
    load_s = time.perf_counter() - t0

    def timed(grain: str, metrics: list[str], group_by: list[str]) -> tuple[dict, float]:
        t = time.perf_counter()
        res = run_query(store, grain=grain, metrics=metrics, group_by=group_by, use_cache=False, publish=False)
        return res, time.perf_counter() - t

    order_res, order_s = timed(
        "order",
        ["order_amount", "item_gross_amount", "item_discount_share", "item_net_amount",
         "item_shipping_share", "discount_amount", "fulfillment_shipping_fee", "pos_count", "order_count"],
        ["tenant"],
    )
    item_res, item_s = timed(
        "item",
        ["item_qty", "item_gross_amount", "item_discount_share", "item_net_amount",
         "item_shipping_share", "item_landed_amount", "pos_count"],
        ["tenant"],
    )
    ful_res, ful_s = timed(
        "fulfillment", ["fulfillment_qty", "fulfillment_shipping_fee", "pos_count"], ["carrier"]
    )
    disc_res, disc_s = timed("discount", ["discount_amount", "pos_count"], ["discount_code"])

    naive_total = sum(d["naive_rows"] for d in [order_res["fanout"]])
    materials = {
        "documents": len(docs),
        "arrays": [args.items, args.discounts, args.fulfillments],
        "empty_orders": sum(1 for d in docs if not d["items"]),
        "load_seconds": round(load_s, 3),
        "load": {"written": load.written, "unchanged": load.unchanged},
        "naive_join_rows": naive_total,
        "naive_orders_dropped": order_res["fanout"]["orders_dropped_by_inner_join"],
        "order_grain_rows": len(order_res["rows"]),
        "item_grain_rows": item_res["totals"]["pos_count"],
        "fulfillment_grain_rows": ful_res["totals"]["pos_count"],
        "discount_grain_rows": disc_res["totals"]["pos_count"],
        "timings_seconds": {
            "order_grain": round(order_s, 4),
            "item_grain": round(item_s, 4),
            "fulfillment_grain": round(ful_s, 4),
            "discount_grain": round(disc_s, 4),
        },
        "checks": {
            "order_amount_total": order_res["totals"]["order_amount"],
            "item_gross_total": item_res["totals"]["item_gross_amount"],
            "item_gross_equals_order_total": order_res["totals"]["order_amount"]
            == item_res["totals"]["item_gross_amount"],
            "naive_item_gross_inflated": order_res["fanout"]["naive_item_gross"],
            "naive_inflation_ratio": round(
                order_res["fanout"]["naive_item_gross"] / max(1, item_res["totals"]["item_gross_amount"]), 2
            ),
        },
        "sample_rows_item": item_res["rows"][:2],
        "sample_rows_order": order_res["rows"][:1],
    }
    json.dump(materials, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    store.close()
    os.unlink(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
