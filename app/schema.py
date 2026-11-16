"""Canonical schema for nested order documents and analysis grains.

The store keeps an append-only, versioned projection of three sibling arrays
inside one order document.  Array identity is always the *element position*
(``<array>[ord]``), never a business id and never the row values: the same
discount code can legitimately appear twice, and two shipments can have the
same carrier and fee.
"""

from __future__ import annotations

from dataclasses import dataclass

BRIDGE_SCHEMA_VERSION = "orders.v3"
CANONICAL_SCHEMA_VERSION = "orders.v3"
SUPPORTED_SOURCE_SCHEMAS = ("orders.v1", "orders.v2", "orders.v3")


@dataclass(frozen=True)
class Grain:
    name: str
    label: str
    array: str
    id_field: str
    element_label: str

    @property
    def is_order(self) -> bool:
        return self.array == ""

    def key_for(self, order_key: str, ord_: int) -> str:
        if self.is_order:
            return f"order:{order_key}"
        return f"{self.name}:{order_key}/{self.array}[{ord_}]"

    def position_for(self, ord_: int) -> str:
        if self.is_order:
            return "$"
        return f"$.{self.array}[{ord_}]"


GRAINS = {
    "order": Grain("order", "订单", "", "order_id", "订单"),
    "item": Grain("item", "商品", "items", "item_id", "商品明细"),
    "fulfillment": Grain("fulfillment", "履约", "fulfillments", "fulfillment_id", "履约元素"),
    "discount": Grain("discount", "优惠", "discounts", "discount_id", "优惠元素"),
}
GRAIN_KEYS = tuple(GRAINS)
GRAIN_LABEL = {name: grain.label for name, grain in GRAINS.items()}
ARRAY_GRAINS = ("item", "fulfillment", "discount")
GRAIN_TO_ARRAY = {g: GRAINS[g].array for g in ARRAY_GRAINS}
ARRAY_TO_GRAIN = {v: k for k, v in GRAIN_TO_ARRAY.items()}

# Canonical root fields.  Every output row carries these so a result can always
# be traced back to its order.
ROOT_FIELDS = (
    "order_id",
    "tenant",
    "channel",
    "status",
    "placed_at",
    "currency",
)

# Map source-schema names onto canonical names.  The normalizer handles nested
# differences; this table is also useful for diagnostics in the UI.
SOURCE_ROOT_ALIASES = {
    "orders.v1": {
        "shop": "tenant",
        "state": "status",
        "created_at": "placed_at",
        "total": "order_amount",
    },
    "orders.v2": {
        "shop": "tenant",
        "placed_at": "placed_at",
        "amount": "order_amount",
    },
    "orders.v3": {},
}

# Column definitions exposed by the web API for rendering.
IDENTITY_COLUMNS = {
    "order": [("order_id", "订单号"), ("tenant", "租户"), ("status", "状态"), ("placed_at", "下单时间")],
    "item": [
        ("order_id", "订单号"),
        ("item_id", "商品 ID"),
        ("sku", "SKU"),
        ("title", "商品"),
        ("unit_price_cents", "单价(分)"),
        ("qty", "件数"),
        ("position", "原始位置"),
    ],
    "fulfillment": [
        ("order_id", "订单号"),
        ("fulfillment_id", "履约 ID"),
        ("carrier", "承运商"),
        ("tracking_no", "运单号"),
        ("line_count", "行数"),
        ("position", "原始位置"),
    ],
    "discount": [
        ("order_id", "订单号"),
        ("discount_id", "优惠 ID"),
        ("code", "优惠码"),
        ("kind", "类型"),
        ("line_count", "元素"),
        ("position", "原始位置"),
    ],
}
