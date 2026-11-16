"""Metric catalogue: what each metric means, at which grain it is native, and
whether it may be rolled up.

Three rigid rules drive the whole planner:

1. A metric may only be SUMmed at a grain where every source fact contributes
   at most once.  Membership in ``additive_at`` is therefore a data-model
   statement, not a UI preference.
2. ``derived`` metrics are recomputed at the output grain from the raw bridge
   at that grain.  They are never produced by scaling another grain's number.
3. Dimensions are never added.  They only define grouping / drill-down.

The catalogue intentionally does *not* declare SUM(discount_amount) legal at
item or fulfillment grain: a discount is not owned by a product line or a
shipment, so attributing it there requires an allocation rule, and an
allocation that happens to add back to the total is not the same thing as an
additive metric.
"""

from __future__ import annotations

from .schema import GRAIN_KEYS

# kind:
#   additive  - SUM is meaningful at the listed grains
#   derived   - recomputed at output grain from components
#   dimension - grouping / filter attribute, never summed
METRICS: dict[str, dict] = {
    # ------------------------------------------------------------------ order
    "order_amount": {
        "label": "订单总额",
        "kind": "additive",
        "native": "order",
        "additive_at": ["order"],
        "unit": "cent",
        "summary": "订单头金额，只属于订单本身；不是三条数组金额的合计。",
        "note": "跨粒度只能先按 order_id 去重再求和；商品/履约粒度不提供该指标。",
    },
    "item_qty": {
        "label": "商品件数",
        "kind": "additive",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "piece",
        "summary": "商品明细行的数量。",
        "note": "履约件数不参与，避免把同一件商品按多个履约行重复计入。",
    },
    "item_gross_amount": {
        "label": "商品原价金额",
        "kind": "additive",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "cent",
        "summary": "unit_price_cents * qty，按商品行计。",
        "note": "订单粒度求和时必须先回到商品行去重。",
    },
    "item_discount_share": {
        "label": "商品分摊优惠",
        "kind": "derived",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "cent",
        "depends_on": ["discount_amount", "item_gross_amount"],
        "summary": "把订单内全部优惠元素按商品原价比例分摊到商品行（最大余额法，确定性）。",
        "note": "优惠元素重复出现时按元素逐条参与分摊；不是 SUM 出来的跨粒度指标。",
    },
    "item_net_amount": {
        "label": "商品净额",
        "kind": "derived",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "cent",
        "depends_on": ["item_gross_amount", "item_discount_share"],
        "summary": "商品原价金额 - 商品分摊优惠。",
        "note": "在商品粒度重算；不允许把订单净额按比例摊回商品。",
    },
    "item_shipping_share": {
        "label": "商品分摊运费",
        "kind": "derived",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "cent",
        "depends_on": ["fulfillment_shipping_fee", "item_qty"],
        "summary": "把订单内全部履约运费按商品件数分摊到商品行（最大余额法）。",
        "note": "没有履约记录时每一项都是 0，而不是 NULL 或跳过订单。",
    },
    "item_landed_amount": {
        "label": "商品到手成本",
        "kind": "derived",
        "native": "item",
        "additive_at": ["item", "order"],
        "unit": "cent",
        "depends_on": ["item_net_amount", "item_shipping_share"],
        "summary": "商品净额 + 商品分摊运费。",
        "note": "只由商品粒度重算；不继承履约行的金额。",
    },
    "discount_amount": {
        "label": "优惠金额",
        "kind": "additive",
        "native": "discount",
        # The same discount id may repeat as separate elements; only the
        # element grain guarantees each element counted once.
        "additive_at": ["discount", "order"],
        "unit": "cent",
        "summary": "优惠数组每个元素的金额之和。",
        "note": "重复的优惠码是真实重复，必须逐元素计数；不可按 code 去重。",
    },
    "fulfillment_shipping_fee": {
        "label": "履约运费",
        "kind": "additive",
        "native": "fulfillment",
        "additive_at": ["fulfillment", "order"],
        "unit": "cent",
        "summary": "履约数组每个元素的运费之和。",
        "note": "同一运单可以有多条履约元素；按元素计数，不按 tracking_no 去重。",
    },
    "fulfillment_qty": {
        "label": "履约件数",
        "kind": "additive",
        "native": "fulfillment",
        "additive_at": ["fulfillment", "order"],
        "unit": "piece",
        "summary": "履约元素声明的件数之和。",
        "note": "与商品件数不可直接相加；两者口径不同，跨口径只能并列展示。",
    },
    "pos_count": {
        "label": "行数（原始位置数）",
        "kind": "additive",
        "native": "order",
        "additive_at": ["order", "item", "fulfillment", "discount"],
        "unit": "row",
        "summary": "结果覆盖的原始嵌套位置数量，每个位置只计一次。",
        "note": "它统计的是真实重复（如同一优惠码出现两次是两个位置），不做业务去重。",
    },
    "order_count": {
        "label": "订单数（去重）",
        "kind": "derived",
        "native": "order",
        "additive_at": [],
        "unit": "order",
        "depends_on": [],
        "summary": "分组内不同 order_id 的数量，按输出粒度重新计算。",
        "note": "这是唯一允许跨粒度“去重”的指标：它数的是订单，不是金额行；其他重复必须保留。",
    },
}

DIMENSIONS: dict[str, dict] = {
    "order": {"label": "订单号", "field": "order_id"},
    "tenant": {"label": "租户", "field": "tenant"},
    "channel": {"label": "渠道", "field": "channel"},
    "status": {"label": "状态", "field": "status"},
    "placed_date": {"label": "下单日期", "field": "placed_date"},
    "sku": {"label": "SKU", "field": "sku"},
    "title": {"label": "商品名称", "field": "title"},
    "carrier": {"label": "承运商", "field": "carrier"},
    "discount_code": {"label": "优惠码", "field": "code"},
    "discount_kind": {"label": "优惠类型", "field": "kind"},
}

# A dimension is only available when the row's grain actually carries it.
DIMENSION_GRAINS = {
    "order": GRAIN_KEYS,
    "tenant": GRAIN_KEYS,
    "channel": GRAIN_KEYS,
    "status": GRAIN_KEYS,
    "placed_date": GRAIN_KEYS,
    "sku": ("item",),
    "title": ("item",),
    "carrier": ("fulfillment",),
    "discount_code": ("discount",),
    "discount_kind": ("discount",),
}

LIFECYCLE = {
    "order": {"gt": "deleted_at", "gte": "created_at", "lte": "updated_at"},
    "item": {"gt": "deleted_at", "gte": "created_at", "lte": "updated_at"},
    "fulfillment": {"gt": "deleted_at", "gte": "created_at", "lte": "updated_at"},
    "discount": {"gt": "deleted_at", "gte": "created_at", "lte": "updated_at"},
}

GRAIN_LABEL = {
    "order": "订单",
    "item": "商品",
    "fulfillment": "履约",
    "discount": "优惠",
}


def metric_names() -> list[str]:
    return list(METRICS)


def metrics_for_grain(grain: str) -> list[str]:
    """Native metrics for a grain (used to preselect the UI)."""
    return [m for m, spec in METRICS.items() if spec["native"] == grain]


def dimensions_for_grain(grain: str) -> list[str]:
    return [d for d, grains in DIMENSION_GRAINS.items() if grain in grains]


def is_additive_at(metric: str, grain: str) -> bool:
    return grain in METRICS[metric].get("additive_at", [])


def catalogue() -> dict:
    return {
        "metrics": {
            name: {
                "label": spec["label"],
                "kind": spec["kind"],
                "native": spec["native"],
                "additive_at": list(spec["additive_at"]),
                "unit": spec["unit"],
                "summary": spec["summary"],
                "note": spec["note"],
                "depends_on": list(spec.get("depends_on", [])),
            }
            for name, spec in METRICS.items()
        },
        "dimensions": DIMENSIONS,
        "dimension_grains": {k: list(v) for k, v in DIMENSION_GRAINS.items()},
        "grains": {
            g: {"label": GRAIN_LABEL[g], "metrics": metrics_for_grain(g), "dimensions": dimensions_for_grain(g)}
            for g in GRAIN_KEYS
        },
    }
