"""Metric execution, hand-checkable baselines, and grain legality.

Every expected number below is derived by hand from data/orders.json and is
written as an arithmetic expression in the test so it can be checked without
running the code.
"""

from __future__ import annotations

from app.executor import QueryError, largest_remainder, metric_reference, run_query
from app.metrics import METRICS
from .conftest import fresh_store


def query(store, **kwargs):
    kwargs.setdefault("use_cache", False)
    kwargs.setdefault("publish", False)
    return run_query(store, **kwargs)


def test_hand_checked_order_grain_totals():
    store = fresh_store()
    result = query(
        store,
        grain="order",
        metrics=[
            "order_amount",
            "item_gross_amount",
            "item_discount_share",
            "item_net_amount",
            "item_shipping_share",
            "item_landed_amount",
            "discount_amount",
            "fulfillment_shipping_fee",
            "fulfillment_qty",
            "pos_count",
            "order_count",
        ],
    )
    t = result["totals"]
    # ORD-1001 10000 + ORD-1002 5000 + ORD-1003 1200 + ORD-1004 0 + ORD-1005 10000
    # + ORD-1007 6000 + ORD-1008 1500 + ORD-2001 1600 + ORD-2002 9000
    assert t["order_amount"] == 10000 + 5000 + 1200 + 0 + 10000 + 6000 + 1500 + 1600 + 9000 == 44300
    # item gross: ORD-1001 2*2000+1*3000+3*1000=10000; 1002 2*2500=5000; 1003 1200;
    # 1005 1*5000+2*2500=10000; 1007 4*1500=6000; 2001 2*800=1600; 2002 2*3000+2*1500=9000
    assert t["item_gross_amount"] == 10000 + 5000 + 1200 + 10000 + 6000 + 1600 + 9000 == 42800
    # discounts: 1000+500+500 + 200 + 300 + 1000+1000 + 200 + 450 = 5150
    assert t["discount_amount"] == 1000 + 500 + 500 + 200 + 300 + 2000 + 200 + 450 == 5150
    # allocation always adds back: net = gross - discount share
    assert t["item_net_amount"] == t["item_gross_amount"] - t["item_discount_share"] == 37950
    assert t["item_landed_amount"] == t["item_net_amount"] + t["item_shipping_share"] == 41150
    # shipping: ORD-1001 800+400, 1003 300, 1004 250, 1007 500+300, 2001 300, 2002 600
    assert t["fulfillment_shipping_fee"] == (800 + 400) + 300 + 250 + (500 + 300) + 300 + 600 == 3450
    # item-level shares can only include orders that have item rows:
    # 1001 1200 + 1003 300 + 1007 800 + 2001 300 + 2002 600 = 3200 (1004 has no items)
    assert t["item_shipping_share"] == 1200 + 300 + 800 + 300 + 600 == 3200
    # fulfillment qty: 1001 lines 2+4, 1003 1, 1004 0, 1007 2+2, 2001 2, 2002 4
    assert t["fulfillment_qty"] == 6 + 1 + 0 + 4 + 2 + 4 == 17
    assert t["pos_count"] == 9
    assert t["order_count"] == 9


def test_item_grain_is_per_position_and_reconciles():
    store = fresh_store()
    result = query(
        store,
        grain="item",
        metrics=["item_qty", "item_gross_amount", "item_discount_share",
                 "item_net_amount", "item_shipping_share", "item_landed_amount"],
        group_by=["order"],
    )
    by_order = {r["group"]["order"]: r["metrics"] for r in result["rows"]}
    assert by_order["ORD-1001"] == {
        "item_qty": 6,
        "item_gross_amount": 10000,
        "item_discount_share": 2000,
        "item_net_amount": 8000,
        "item_shipping_share": 1200,
        "item_landed_amount": 9200,
    }
    assert by_order["ORD-2002"]["item_discount_share"] == 450
    assert by_order["ORD-2002"]["item_net_amount"] == 9000 - 450
    # empty item arrays produce no rows
    assert "ORD-1004" not in by_order
    assert "ORD-1008" not in by_order
    skipped = {s["order_id"] for s in result["skipped_orders"]}
    assert skipped == {"ORD-1004", "ORD-1008"}


def test_item_positions_are_traceable_to_original_nesting():
    store = fresh_store()
    result = query(
        store,
        grain="item",
        metrics=["item_discount_share", "item_shipping_share"],
        group_by=["order"],
        detail=True,
    )
    details = [r for r in result["details"] if r["order_id"] == "ORD-2002"]
    # ORD-2002 has two item positions; each detail row carries exactly one.
    assert [r["positions"][0]["position"] for r in details] == ["$.items[0]", "$.items[1]"]
    # each row's derived discount share points at the exact discount element
    for row in details:
        assert {p["position"] for p in row["derived_from"]["item_discount_share"]} == {"$.discounts[0]"}
        assert {p["position"] for p in row["derived_from"]["item_shipping_share"]} == {"$.fulfillments[0]"}

    summary = next(r for r in result["rows"] if r["group"]["order"] == "ORD-1001")
    assert {p["position"] for p in summary["provenance"]["positions"]} == {
        "$.items[0]",
        "$.items[1]",
        "$.items[2]",
    }
    assert {p["position"] for p in summary["provenance"]["derived_from"]["item_discount_share"]} == {
        "$.discounts[0]",
        "$.discounts[1]",
        "$.discounts[2]",
    }
    assert {p["position"] for p in summary["provenance"]["derived_from"]["item_shipping_share"]} == {
        "$.fulfillments[0]",
        "$.fulfillments[1]",
    }


def test_duplicate_discount_code_is_not_deduped():
    store = fresh_store()
    result = query(store, grain="discount", metrics=["discount_amount", "pos_count"], group_by=["discount_code"])
    by_code = {r["group"]["discount_code"]: r for r in result["rows"]}
    welcome = by_code["WELCOME"]
    assert welcome["metrics"]["discount_amount"] == 1000 + 500
    assert welcome["metrics"]["pos_count"] == 2  # two real positions
    assert "duplicate_discount_code" in welcome["provenance"]["flags"]
    assert {p["position"] for p in welcome["provenance"]["positions"]} == {
        "$.discounts[0]",
        "$.discounts[1]",
    }
    # grouping by code is presentation only: the grand total is unchanged
    assert result["totals"]["discount_amount"] == 5150
    # ORD-1001 3 + 1003 1 + 1004 1 + 1005 2 + 2001 1 + 2002 1 = 9 positions
    assert result["totals"]["pos_count"] == 9


def test_cross_grain_sums_agree_without_deduping():
    store = fresh_store()
    order_totals = query(
        store,
        grain="order",
        metrics=["discount_amount", "fulfillment_shipping_fee"],
    )["totals"]
    discount_totals = query(store, grain="discount", metrics=["discount_amount"])["totals"]
    fulfillment_totals = query(store, grain="fulfillment", metrics=["fulfillment_shipping_fee"])["totals"]
    assert order_totals["discount_amount"] == discount_totals["discount_amount"] == 5150
    assert (
        order_totals["fulfillment_shipping_fee"]
        == fulfillment_totals["fulfillment_shipping_fee"]
        == 3450
    )


def test_allocation_preserves_total_and_is_deterministic():
    assert largest_remainder(100, [1, 1, 1]) == [34, 33, 33]
    assert largest_remainder(100, [1, 1, 1]) == [34, 33, 33]
    assert sum(largest_remainder(5150, [3, 5, 11, 7])) == 5150
    assert largest_remainder(0, [1, 2]) == [0, 0]
    assert largest_remainder(10, [0, 0]) == [10, 0]
    assert largest_remainder(10, []) == []


def test_empty_arrays_are_zero_not_missing():
    store = fresh_store()
    result = query(
        store,
        grain="item",
        metrics=["item_shipping_share", "item_discount_share"],
        group_by=["order"],
    )
    by_order = {r["group"]["order"]: r["metrics"] for r in result["rows"]}
    # ORD-1005 has discounts but no fulfillments
    assert by_order["ORD-1005"]["item_shipping_share"] == 0
    assert by_order["ORD-1005"]["item_discount_share"] == 2000
    # ORD-1007 has fulfillments but no discounts
    assert by_order["ORD-1007"]["item_shipping_share"] == 800
    assert by_order["ORD-1007"]["item_discount_share"] == 0


def test_illegal_metric_grain_combinations_are_rejected():
    store = fresh_store()
    cases = [
        ("item", "discount_amount", "优惠金额"),
        ("item", "order_amount", "订单总额"),
        ("fulfillment", "item_qty", "商品件数"),
        ("fulfillment", "discount_amount", "优惠金额"),
        ("discount", "item_gross_amount", "商品原价金额"),
        ("discount", "fulfillment_shipping_fee", "履约运费"),
    ]
    for grain, metric, label in cases:
        try:
            query(store, grain=grain, metrics=[metric])
        except QueryError as exc:
            assert label in str(exc), (grain, metric, str(exc))
            assert any(d["metric"] == metric and not d["accepted"] for d in exc.details)
        else:
            raise AssertionError(f"{grain}/{metric} should be rejected")


def test_allowed_combinations_include_native_and_additive_rollups():
    ref = metric_reference()
    allowed = {
        (grain, entry["metric"])
        for grain, block in ref.items()
        for entry in block["metrics"]
        if entry["accepted"]
    }
    # native
    assert ("item", "item_gross_amount") in allowed
    assert ("fulfillment", "fulfillment_shipping_fee") in allowed
    assert ("discount", "discount_amount") in allowed
    # rolled up to order
    assert ("order", "item_gross_amount") in allowed
    assert ("order", "discount_amount") in allowed
    # derived metrics are never legal at discount/fulfillment grain
    assert ("discount", "item_net_amount") not in allowed
    assert ("fulfillment", "item_landed_amount") not in allowed


def test_filter_dimension_must_exist_on_output_grain():
    store = fresh_store()
    try:
        query(store, grain="item", metrics=["item_gross_amount"], filters={"carrier": "SF"})
    except QueryError as exc:
        assert "承运商" in str(exc)
    else:
        raise AssertionError("carrier filter on item grain should be rejected")
    # valid filter
    result = query(store, grain="item", metrics=["item_gross_amount"], filters={"order": "ORD-1001"})
    assert result["totals"]["item_gross_amount"] == 10000


def test_fanout_report_matches_hand_count():
    store = fresh_store()
    result = query(store, grain="order", metrics=["pos_count"])
    fanout = result["fanout"]
    # non-empty triples: 1001 3*3*2=18, 1003 1, 2001 1, 2002 2*1*1=2
    assert fanout["naive_rows"] == 18 + 1 + 1 + 2 == 22
    # orders with items but at least one empty array: 1002, 1005, 1007
    assert fanout["orders_dropped_by_inner_join"] == 3
    # inflated gross only for triple-complete orders:
    # 10000*3*2 + 1200 + 1600 + 9000 = 71800 (true: 21800)
    assert fanout["naive_item_gross"] == 60000 + 1200 + 1600 + 9000 == 71800
    assert result["totals"]["pos_count"] == 9  # executor rows are per order


def test_metrics_catalogue_is_internally_consistent():
    for metric, spec in METRICS.items():
        assert spec["kind"] in {"additive", "derived"}
        if spec["kind"] == "derived" and metric != "order_count":
            assert spec["additive_at"], metric
        assert spec["native"] in {"order", "item", "fulfillment", "discount"}
        # additive metrics must name at least one legal grain
        if spec["kind"] == "additive":
            assert spec["additive_at"], metric
