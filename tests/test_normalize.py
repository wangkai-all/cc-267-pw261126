"""Normalization across schema versions and element identity rules."""

from __future__ import annotations

from app.normalize import (
    NormalizationError,
    fingerprint_document,
    normalize_document,
)

from .conftest import base_docs, doc


def test_v3_integers_are_cents():
    order = normalize_document(doc("ORD-1001"))
    assert order.order_amount == 10000
    assert order.items[0].payload["unit_price_cents"] == 2000
    assert order.discounts[0].payload["amount_cents"] == 1000
    assert order.fulfillments[0].payload["shipping_fee_cents"] == 800
    assert order.fulfillments[1].payload["qty"] == 4  # lines: 1 + 3


def test_v1_string_money_and_aliases():
    order = normalize_document(doc("ORD-2001"))
    assert order.tenant == "shop-legacy"  # from `shop`
    assert order.status == "paid"  # from `state`
    assert order.placed_at.isoformat().startswith("2026-09-20T03:00:00")
    assert order.order_amount == 1600
    assert order.items[0].payload["unit_price_cents"] == 800
    assert order.discounts[0].payload["amount_cents"] == 200
    assert order.fulfillments[0].payload["shipping_fee_cents"] == 300
    assert order.fulfillments[0].payload["qty"] == 2


def test_v2_percent_discount_and_nested_shipping():
    order = normalize_document(doc("ORD-2002"))
    assert order.order_amount == 9000
    # 5% of base_cents=9000 = 450
    assert order.discounts[0].payload["amount_cents"] == 450
    # shipping.fee="600" is yuan-ish string with integer digits -> cents
    assert order.fulfillments[0].payload["shipping_fee_cents"] == 600
    assert order.fulfillments[0].payload["qty"] == 4


def test_positions_are_stable_and_unique():
    for raw in base_docs():
        order = normalize_document(raw)
        positions = [e.payload["position"] for e in order.items]
        assert positions == [f"$.items[{i}]" for i in range(len(order.items))]
        assert len({e.payload["position"] for e in order.discounts}) == len(order.discounts)
        assert len({e.payload["position"] for e in order.fulfillments}) == len(order.fulfillments)


def test_duplicate_discount_code_keeps_distinct_elements():
    order = normalize_document(doc("ORD-1001"))
    codes = [d.payload["code"] for d in order.discounts]
    assert codes.count("WELCOME") == 2
    assert order.discounts[0].business_id != order.discounts[1].business_id
    assert order.discounts[0].ord != order.discounts[1].ord


def test_missing_business_id_gets_positional_identity():
    raw = {
        "schema_version": "orders.v3",
        "order_id": "X1",
        "tenant": "t",
        "placed_at": "2026-01-01T00:00:00Z",
        "order_amount": 100,
        "items": [
            {"qty": 1, "unit_price_cents": 100},
            {"sku": "A", "qty": 1, "unit_price_cents": 100},
        ],
        "discounts": [{"code": "N", "amount_cents": 10}],
        "fulfillments": [{"carrier": "SF", "shipping_fee_cents": 0, "lines": []}],
    }
    order = normalize_document(raw)
    # No item_id and no sku -> positional synthetic id.
    assert order.items[0].business_id == "__pos_0"
    # No item_id but a sku -> the sku is the business id.
    assert order.items[1].business_id == "A"
    assert order.discounts[0].business_id == "__pos_0"
    assert order.fulfillments[0].business_id == "__pos_0"
    assert order.items[0].payload["position"] == "$.items[0]"


def test_unsupported_schema_rejected():
    raw = doc("ORD-1001")
    raw["schema_version"] = "orders.v9"
    try:
        normalize_document(raw)
    except NormalizationError as exc:
        assert "unsupported schema_version" in str(exc)
    else:
        raise AssertionError("expected NormalizationError")


def test_non_array_field_rejected():
    raw = doc("ORD-1001")
    raw["discounts"] = {"code": "oops"}
    try:
        normalize_document(raw)
    except NormalizationError as exc:
        assert "discounts" in str(exc)
    else:
        raise AssertionError("expected NormalizationError")


def test_fingerprint_ignores_volatile_keys_but_tracks_content():
    raw = doc("ORD-1001")
    copy = dict(raw)
    copy["ingested_at"] = "2026-11-11T00:00:00Z"
    assert fingerprint_document(raw) == fingerprint_document(copy)
    copy["order_amount"] = 9999
    assert fingerprint_document(raw) != fingerprint_document(copy)
