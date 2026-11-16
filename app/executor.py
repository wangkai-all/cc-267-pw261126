"""Query validation and execution.

Strategy: *aggregate first, then associate at order grain* (先聚合再关联),
with a single optional expansion to the output grain.

For an output grain that is an array (item / fulfillment / discount) the
executor starts from the order-grain bridge row and expands **one** array
only.  Facts belonging to the other two arrays are first reduced to totals at
order grain and then, where the metric definition calls for it, allocated
deterministically (integer largest-remainder) to the output positions.  This
is why a three-way multiplicative blow-up never happens, while every output
row still carries the original nested positions of both itself and the facts
it derives from.

No result row is ever deduplicated.  If the same discount code appears twice,
it is two positions and two contributions; grouping by code only changes the
presentation and the ``pos_count`` still shows 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .metrics import DIMENSION_GRAINS, DIMENSIONS, METRICS, metrics_for_grain
from .plan import query_digest
from .schema import GRAIN_KEYS, GRAIN_LABEL
from .store import Store

ROOT_DIMENSIONS = ("order", "tenant", "channel", "status", "placed_date")
GRAIN_DIMENSIONS = {
    "order": (),
    "item": ("sku", "title"),
    "fulfillment": ("carrier",),
    "discount": ("discount_code", "discount_kind"),
}


class QueryError(ValueError):
    """The requested grain/metric/grouping combination is not legal."""

    def __init__(self, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.details = details or []


@dataclass
class OrderContext:
    order_key: str
    version: int
    projection: int
    plan_hash: str
    root: dict
    items: list[dict]
    discounts: list[dict]
    fulfillments: list[dict]
    edge_count: int

    @property
    def items_by_ord(self) -> dict[int, dict]:
        return {r["element_ord"]: r for r in self.items}


def _load_contexts(snap, order_keys: Iterable[str] | None = None) -> list[OrderContext]:
    states = snap.latest_states()
    if order_keys is not None:
        wanted = set(order_keys)
        states = [s for s in states if s["order_key"] in wanted]
    contexts = []
    for state in states:
        rows = snap.rows_for(state["order_key"], int(state["version"]))
        grouped: dict[str, list[dict]] = {"order": [], "item": [], "discount": [], "fulfillment": []}
        for row in rows:
            grouped[row["grain"]].append(
                {
                    "grain": row["grain"],
                    "ord": int(row["element_ord"]),
                    "source_id": row["source_id"],
                    "array_size": int(row["array_size"]),
                    "payload": json.loads(row["payload"]),
                }
            )
        plan = snap.plan_for(state["order_key"])
        contexts.append(
            OrderContext(
                order_key=state["order_key"],
                version=int(state["version"]),
                projection=int(state["projection"]),
                plan_hash=plan["plan_hash"] if plan else "",
                root=grouped["order"][0]["payload"],
                items=grouped["item"],
                discounts=grouped["discount"],
                fulfillments=grouped["fulfillment"],
                edge_count=snap.edge_count(state["order_key"], int(state["version"])),
            )
        )
    return contexts


# ---------------------------------------------------------------------------
# Deterministic integer allocation
# ---------------------------------------------------------------------------


def largest_remainder(amount: int, weights: list[int]) -> list[int]:
    """Split ``amount`` cents across ``weights`` with no cent lost or created.

    Deterministic ties are broken by lower list index.  All-zero weights put
    the whole amount on index 0 and mark the case with a warning.
    """
    if not weights:
        return []
    total = sum(weights)
    if total <= 0:
        shares = [0] * len(weights)
        shares[0] = amount
        return shares
    raw = [amount * w for w in weights]
    shares = [v // total for v in raw]
    remainder = amount - sum(shares)
    # Largest fractional remainder first; stable on index.
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] % total), i))
    for i in order[:remainder]:
        shares[i] += 1
    return shares


# ---------------------------------------------------------------------------
# Base fact rows at each grain
# ---------------------------------------------------------------------------


def _root_dims(ctx: OrderContext) -> dict:
    return {
        "order": ctx.root.get("order_id", ""),
        "tenant": ctx.root.get("tenant", ""),
        "channel": ctx.root.get("channel", ""),
        "status": ctx.root.get("status", ""),
        "placed_date": ctx.root.get("placed_date", ""),
    }


def _position_of(row: dict) -> str:
    return row["payload"].get("position", "")


def _trace_of(rows: Iterable[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append(
            {
                "position": _position_of(r),
                "source_id": r["source_id"],
                "grain": r["grain"],
            }
        )
    return out


def item_base_rows(ctx: OrderContext) -> list[dict]:
    """One row per item position, with derived metrics recomputed here."""
    items = ctx.items
    gross = [int(i["payload"]["unit_price_cents"]) * int(i["payload"]["qty"]) for i in items]
    qty = [int(i["payload"]["qty"]) for i in items]
    discount_total = sum(int(d["payload"]["amount_cents"]) for d in ctx.discounts)
    shipping_total = sum(int(f["payload"]["shipping_fee_cents"]) for f in ctx.fulfillments)

    discount_shares = largest_remainder(discount_total, gross)
    shipping_weights = qty if sum(qty) > 0 else gross
    shipping_shares = largest_remainder(shipping_total, shipping_weights)

    discount_trace = _trace_of(ctx.discounts)
    fulfillment_trace = _trace_of(ctx.fulfillments)
    base_dims = _root_dims(ctx)
    out = []
    for idx, item in enumerate(items):
        p = item["payload"]
        g = gross[idx]
        ds = discount_shares[idx] if discount_shares else 0
        net = g - ds
        ss = shipping_shares[idx] if shipping_shares else 0
        landed = net + ss
        dims = {**base_dims, "sku": p.get("sku", ""), "title": p.get("title", "")}
        flags = []
        if not ctx.discounts:
            flags.append("no_discounts")
        if not ctx.fulfillments:
            flags.append("no_fulfillments")
        if net < 0:
            flags.append("negative_net_amount")
        out.append(
            {
                "grain": "item",
                "dims": dims,
                "metrics": {
                    "item_qty": int(p["qty"]),
                    "item_gross_amount": g,
                    "item_discount_share": ds,
                    "item_net_amount": net,
                    "item_shipping_share": ss,
                    "item_landed_amount": landed,
                    "pos_count": 1,
                },
                "order_id": base_dims["order"],
                "positions": [_trace_of([item])[0]],
                "derived_from": {
                    "item_discount_share": discount_trace,
                    "item_shipping_share": fulfillment_trace,
                },
                "flags": flags,
            }
        )
    return out


def fulfillment_base_rows(ctx: OrderContext) -> list[dict]:
    base_dims = _root_dims(ctx)
    out = []
    for ful in ctx.fulfillments:
        p = ful["payload"]
        dims = {**base_dims, "carrier": p.get("carrier", "")}
        flags = []
        if int(p.get("qty", 0)) <= 0:
            flags.append("zero_fulfillment_qty")
        out.append(
            {
                "grain": "fulfillment",
                "dims": dims,
                "metrics": {
                    "fulfillment_qty": int(p.get("qty", 0)),
                    "fulfillment_shipping_fee": int(p.get("shipping_fee_cents", 0)),
                    "pos_count": 1,
                },
                "order_id": base_dims["order"],
                "positions": [_trace_of([ful])[0]],
                "derived_from": {},
                "flags": flags,
            }
        )
    return out


def discount_base_rows(ctx: OrderContext) -> list[dict]:
    base_dims = _root_dims(ctx)
    out = []
    seen_codes: dict[str, int] = {}
    for d in ctx.discounts:
        p = d["payload"]
        code = p.get("code", "")
        seen_codes[code] = seen_codes.get(code, 0) + 1
        dims = {**base_dims, "discount_code": code, "discount_kind": p.get("kind", "")}
        out.append(
            {
                "grain": "discount",
                "dims": dims,
                "metrics": {"discount_amount": int(p["amount_cents"]), "pos_count": 1},
                "order_id": base_dims["order"],
                "positions": [_trace_of([d])[0]],
                "derived_from": {},
                "flags": [],
            }
        )
    for row in out:
        code = row["dims"]["discount_code"]
        if seen_codes.get(code, 0) > 1:
            row["flags"].append("duplicate_discount_code")
    return out


def order_base_rows(ctx: OrderContext) -> list[dict]:
    items = item_base_rows(ctx)
    fuls = fulfillment_base_rows(ctx)
    discs = discount_base_rows(ctx)
    metrics: dict[str, int] = {
        "order_amount": int(ctx.root.get("order_amount_cents", 0)),
        "pos_count": 1,
        "order_count": 1,
    }
    for key in (
        "item_qty",
        "item_gross_amount",
        "item_discount_share",
        "item_net_amount",
        "item_shipping_share",
        "item_landed_amount",
    ):
        metrics[key] = sum(r["metrics"][key] for r in items)
    metrics["discount_amount"] = sum(r["metrics"]["discount_amount"] for r in discs)
    metrics["fulfillment_qty"] = sum(r["metrics"]["fulfillment_qty"] for r in fuls)
    metrics["fulfillment_shipping_fee"] = sum(
        r["metrics"]["fulfillment_shipping_fee"] for r in fuls
    )
    flags = []
    if not items:
        flags.append("no_items")
    if not discs:
        flags.append("no_discounts")
    if not fuls:
        flags.append("no_fulfillments")
    if sum(r["metrics"]["item_net_amount"] for r in items) < 0:
        flags.append("negative_net_amount")
    return [
        {
            "grain": "order",
            "dims": _root_dims(ctx),
            "metrics": metrics,
            "order_id": ctx.root.get("order_id", ""),
            "positions": [{"position": "$", "source_id": ctx.root.get("order_id", ""), "grain": "order"}],
            "derived_from": {
                "item_qty": _trace_of(ctx.items),
                "item_gross_amount": _trace_of(ctx.items),
                "item_discount_share": _trace_of(ctx.discounts),
                "item_shipping_share": _trace_of(ctx.fulfillments),
                "discount_amount": _trace_of(ctx.discounts),
                "fulfillment_shipping_fee": _trace_of(ctx.fulfillments),
            },
            "flags": flags,
        }
    ]


BASE_BUILDERS = {
    "order": order_base_rows,
    "item": item_base_rows,
    "fulfillment": fulfillment_base_rows,
    "discount": discount_base_rows,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _metric_allowed(metric: str, grain: str) -> tuple[bool, str]:
    spec = METRICS.get(metric)
    if spec is None:
        return False, f"未知指标 {metric}"
    if metric == "order_count":
        return True, "按输出粒度重新计算 distinct order_id"
    if metric == "pos_count":
        return True, "统计原始位置数，每个位置只计一次"
    if metric == "order_amount":
        if grain == "order":
            return True, "订单头金额，订单粒度原生"
        return False, "订单头金额只属于订单本身；在数组粒度出现会重复，必须回到订单粒度"
    if spec["native"] == grain:
        return True, "在该粒度原生计算"
    if grain in spec.get("additive_at", []):
        if spec["kind"] == "derived":
            return True, "在原生粒度重算后按该粒度去重求和"
        return True, "该粒度下每条源事实至多贡献一次，可加"
    if grain in ("item", "fulfillment", "discount"):
        owners = "/".join(GRAIN_LABEL[g] for g in spec["additive_at"] if g != "order") or spec["native"]
        return False, (
            f"不可在{GRAIN_LABEL[grain]}粒度相加：每条事实在 {owners} 粒度只计一次，"
            "跨到该数组会重复；只能回到其原生粒度或订单粒度计算"
        )
    return False, "该指标不支持该粒度"


def validate_query(grain: str, metrics: list[str], group_by: list[str], filters: dict) -> dict:
    if grain not in GRAIN_KEYS:
        raise QueryError(f"未知粒度 {grain!r}", [{"kind": "grain", "message": f"可选：{', '.join(GRAIN_KEYS)}"}])
    accepted, rejected, policy = [], [], []
    seen = set()
    for metric in metrics:
        if metric in seen:
            raise QueryError(f"指标 {metric} 重复", [{"kind": "metric", "metric": metric}])
        seen.add(metric)
        ok, reason = _metric_allowed(metric, grain)
        spec = METRICS.get(metric, {})
        entry = {
            "metric": metric,
            "label": spec.get("label", metric),
            "kind": spec.get("kind", "unknown"),
            "native": spec.get("native", ""),
            "additive_at": spec.get("additive_at", []),
            "accepted": ok,
            "reason": reason,
        }
        policy.append(entry)
        if ok:
            accepted.append(metric)
        else:
            rejected.append(entry)
    if rejected:
        raise QueryError(
            f"{GRAIN_LABEL[grain]}粒度不支持指标：{', '.join(r['label'] for r in rejected)}",
            rejected,
        )
    if not metrics:
        raise QueryError("至少选择一个指标", [{"kind": "metric", "message": "metrics 不能为空"}])

    grain_dims = set(ROOT_DIMENSIONS) | set(GRAIN_DIMENSIONS[grain])
    for dim in group_by:
        if dim not in DIMENSIONS:
            raise QueryError(f"未知维度 {dim}")
        if grain not in DIMENSION_GRAINS.get(dim, ()) or dim not in grain_dims:
            raise QueryError(
                f"{GRAIN_LABEL[grain]}粒度没有维度 {DIMENSIONS[dim]['label']}；"
                f"可用：{', '.join(sorted(grain_dims))}"
            )
    for key in filters:
        if key in ("placed_date_from", "placed_date_to"):
            continue
        if key not in DIMENSIONS:
            raise QueryError(f"未知过滤字段 {key}")
        if grain not in DIMENSION_GRAINS.get(key, ()) or key not in grain_dims:
            raise QueryError(
                f"{GRAIN_LABEL[grain]}粒度不能按 {DIMENSIONS[key]['label']} 过滤；"
                "该维度不在输出行上，按它过滤会改变指标口径"
            )
    return {"accepted": accepted, "rejected": rejected, "policy": policy}


def _dimension_values(dim: str, value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if v not in (None, "")}
    return {str(value)}


def _matches_filters(row: dict, filters: dict) -> bool:
    for key, value in filters.items():
        if key in ("placed_date_from", "placed_date_to"):
            continue
        wanted = _dimension_values(key, value)
        if not wanted:
            continue
        row_value = str(row["dims"].get(key, ""))
        if row_value not in wanted:
            return False
    date_value = str(row["dims"].get("placed_date", ""))
    if filters.get("placed_date_from") and date_value < str(filters["placed_date_from"]):
        return False
    if filters.get("placed_date_to") and date_value > str(filters["placed_date_to"]):
        return False
    return True


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_rows(rows: list[dict], group_by: list[str]) -> list[dict]:
    if not group_by:
        out = []
        for row in rows:
            out.append(
                {
                    "group": {},
                    "metrics": dict(row["metrics"]),
                    "provenance": {
                        "grain": row["grain"],
                        "order_id": row["order_id"],
                        "positions": row["positions"],
                        "derived_from": row["derived_from"],
                        "flags": sorted(set(row["flags"])),
                        "orders": [row["order_id"]],
                        "pos_count": row["metrics"].get("pos_count", 1),
                    },
                }
            )
        return out

    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(str(row["dims"].get(dim, "")) for dim in group_by)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "group": {dim: row["dims"].get(dim, "") for dim in group_by},
                "metrics": {},
                "positions": [],
                "derived_from": {},
                "flags": set(),
                "orders": set(),
                "pos_count": 0,
            }
            buckets[key] = bucket
        for metric, value in row["metrics"].items():
            if metric == "order_count":
                continue
            bucket["metrics"][metric] = bucket["metrics"].get(metric, 0) + int(value)
        bucket["pos_count"] += int(row["metrics"].get("pos_count", 1))
        bucket["positions"].extend(row["positions"])
        bucket["orders"].add(row["order_id"])
        bucket["flags"].update(row["flags"])
        for metric, trace in row["derived_from"].items():
            bucket["derived_from"].setdefault(metric, []).extend(trace)

    out = []
    for key in sorted(buckets):
        bucket = buckets[key]
        bucket["metrics"]["order_count"] = len(bucket["orders"])
        bucket["metrics"]["pos_count"] = bucket["pos_count"]
        out.append(
            {
                "group": bucket["group"],
                "metrics": bucket["metrics"],
                "provenance": {
                    "grain": rows[0]["grain"] if rows else "",
                    "order_id": None,
                    "positions": bucket["positions"],
                    "derived_from": bucket["derived_from"],
                    "flags": sorted(bucket["flags"]),
                    "orders": sorted(bucket["orders"]),
                    "pos_count": bucket["pos_count"],
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# Fan-out benchmark (what the naive join would have done)
# ---------------------------------------------------------------------------

def fanout_report(contexts: list[OrderContext]) -> dict:
    naive_rows = 0
    dropped = 0
    naive_item_gross = 0
    inflated_orders = []
    for ctx in contexts:
        n_items = len(ctx.items)
        n_disc = len(ctx.discounts)
        n_ful = len(ctx.fulfillments)
        product = n_items * n_disc * n_ful
        naive_rows += product
        if n_items and product == 0:
            dropped += 1
        if n_items and n_disc and n_ful:
            gross = sum(
                int(i["payload"]["unit_price_cents"]) * int(i["payload"]["qty"]) for i in ctx.items
            )
            naive_item_gross += gross * n_disc * n_ful
            inflated_orders.append(
                {
                    "order_key": ctx.order_key,
                    "items": n_items,
                    "discounts": n_disc,
                    "fulfillments": n_ful,
                    "rows": product,
                    "inflated_item_gross": gross * n_disc * n_ful,
                    "true_item_gross": gross,
                }
            )
    return {
        "naive_rows": naive_rows,
        "orders_dropped_by_inner_join": dropped,
        "naive_item_gross": naive_item_gross,
        "inflated_orders": inflated_orders[:12],
        "note": (
            "直接三数组展开：行数是 items*discounts*fulfillments，金额按乘积放大；"
            "任一数组为空时内连接会把整个订单丢掉。以上仅作对照，不作为查询结果。"
        ),
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _query_key(
    grain: str, metrics: list[str], group_by: list[str], filters: dict, detail: bool = False
) -> str:
    return query_digest(
        {
            "grain": grain,
            "metrics": sorted(metrics),
            "group_by": list(group_by),
            "filters": filters,
            "detail": bool(detail),
        }
    )


def _publish(store: Store, query_key: str, digest: str, grain: str, plan_set: list[dict], result: dict) -> dict:
    """Publish the result against the observed dataset digest (CAS).

    A stale computation is reported back but never written; the payload keeps
    the newer head intact.
    """
    from .store import StaleResult

    try:
        published = store.publish_result(
            query_key,
            observed_dataset_digest=digest,
            grain=grain,
            plan_set=plan_set,
            payload=result,
        )
    except StaleResult as exc:
        result["warnings"].append(
            {
                "kind": "stale_result",
                "message": str(exc),
                "detail": "结果未发布；请基于最新版本重跑，旧展开结果不会覆盖新粒度。",
            }
        )
        return {"status": "refused", "reason": str(exc)}
    return published


def run_query(
    store: Store,
    *,
    grain: str,
    metrics: list[str],
    group_by: list[str] | None = None,
    filters: dict | None = None,
    use_cache: bool = True,
    publish: bool = True,
    detail: bool = False,
) -> dict:
    group_by = list(group_by or [])
    filters = dict(filters or {})
    validation = validate_query(grain, metrics, group_by, filters)
    query_key = _query_key(grain, metrics, group_by, filters, detail)

    with store.snapshot() as snap:
        result, digest, plan_set = _execute(
            snap,
            grain=grain,
            metrics=metrics,
            group_by=group_by,
            filters=filters,
            validation=validation,
            use_cache=use_cache,
            detail=detail,
        )
    if result.get("cache", {}).get("hit"):
        return result
    if publish:
        result["published"] = _publish(store, query_key, digest, grain, plan_set, result)
    return result


def _execute(
    snap,
    *,
    grain: str,
    metrics: list[str],
    group_by: list[str],
    filters: dict,
    validation: dict,
    use_cache: bool,
    detail: bool = False,
) -> tuple[dict, str, list[dict]]:
    query_key = _query_key(grain, metrics, group_by, filters, detail)
    contexts = _load_contexts(snap)
    digest = snap.current_dataset_digest()
    if use_cache:
        cached = snap.cached_result(query_key, digest)
        if cached is not None:
            cached["cache"] = {"hit": True, "query_key": query_key, "dataset_digest": digest}
            return cached, digest, []
    contexts = [c for c in contexts if _order_matches(c, filters)]
    base_rows: list[dict] = []
    for ctx in contexts:
        base_rows.extend(BASE_BUILDERS[grain](ctx))
    selected = [r for r in base_rows if _matches_filters(r, filters)]
    grouped = _group_rows(selected, group_by)
    for row in grouped:
        prov = row["provenance"]
        full = dict(row["metrics"])
        if "order_count" not in full and prov["orders"]:
            full["order_count"] = len(prov["orders"])
        row["metrics"] = {m: int(full.get(m, 0)) for m in metrics}

    totals = _totals(grouped, metrics)
    fanout = fanout_report(contexts)
    plan_set = [
        {
            "order_key": ctx.order_key,
            "order_version": ctx.version,
            "projection": ctx.projection,
            "plan_hash": ctx.plan_hash,
        }
        for ctx in contexts
    ]
    skipped = _skipped_orders(contexts, grain, selected)
    warnings = _warnings(contexts, grain, filters, totals)
    result = {
        "ok": True,
        "grain": grain,
        "grain_label": GRAIN_LABEL[grain],
        "metrics": metrics,
        "metric_policy": validation["policy"],
        "group_by": group_by,
        "filters": filters,
        "rows": grouped,
        "totals": totals,
        "orders_considered": len(contexts),
        "orders_with_rows": len({o for r in grouped for o in r["provenance"]["orders"]}),
        "skipped_orders": skipped,
        "warnings": warnings,
        "fanout": fanout,
        "plan": {
            "dataset_digest": digest,
            "projection": snap.projection(),
            "order_count": len(contexts),
            "orders": plan_set,
            "strategy": "先按订单聚合各数组，再只展开一个数组；跨数组指标按订单总额分摊",
        },
        "cache": {"hit": False, "query_key": query_key, "dataset_digest": digest},
    }
    if detail:
        result["details"] = [
            {
                "order_id": row["order_id"],
                "positions": row["positions"],
                "metrics": {m: int(row["metrics"].get(m, 0)) for m in metrics if m in row["metrics"]},
                "derived_from": row["derived_from"],
                "flags": sorted(set(row["flags"])),
            }
            for row in selected
        ]
    return result, digest, plan_set


def _order_matches(ctx: OrderContext, filters: dict) -> bool:
    root = ctx.root
    map_ = {
        "order": "order_id",
        "tenant": "tenant",
        "channel": "channel",
        "status": "status",
    }
    for key, field in map_.items():
        if key not in filters:
            continue
        wanted = _dimension_values(key, filters[key])
        if wanted and str(root.get(field, "")) not in wanted:
            return False
    date = str(root.get("placed_date", ""))
    if filters.get("placed_date_from") and date < str(filters["placed_date_from"]):
        return False
    if filters.get("placed_date_to") and date > str(filters["placed_date_to"]):
        return False
    return True


def _totals(rows: list[dict], metrics: list[str]) -> dict:
    totals: dict[str, int] = {m: 0 for m in metrics}
    order_ids: set[str] = set()
    for row in rows:
        for metric in metrics:
            if metric == "order_count":
                continue
            totals[metric] += int(row["metrics"].get(metric, 0))
        order_ids.update(row["provenance"]["orders"])
    if "order_count" in metrics:
        totals["order_count"] = len(order_ids)
    return totals


def _skipped_orders(contexts: list[OrderContext], grain: str, selected: list[dict]) -> list[dict]:
    if grain == "order":
        return []
    present = {row["order_id"] for row in selected}
    out = []
    for ctx in contexts:
        order_id = ctx.root.get("order_id")
        counts = {
            "items": len(ctx.items),
            "discounts": len(ctx.discounts),
            "fulfillments": len(ctx.fulfillments),
        }
        empty = counts[{"item": "items", "discount": "discounts", "fulfillment": "fulfillments"}[grain]] == 0
        if empty:
            reason = f"{GRAIN_LABEL[grain]}数组为空；该订单在{GRAIN_LABEL[grain]}粒度没有输出行"
        elif order_id not in present:
            reason = "该订单在当前数组粒度没有匹配输出行（被维度过滤排除）"
        else:
            continue
        out.append({"order_key": ctx.order_key, "order_id": order_id, "counts": counts, "reason": reason})
    return out


def _warnings(contexts: list[OrderContext], grain: str, filters: dict, totals: dict) -> list[dict]:
    warnings: list[dict] = []
    empty_arrays = [
        {"order_key": c.order_key, "empty": [
            name for name, arr in (("items", c.items), ("discounts", c.discounts), ("fulfillments", c.fulfillments))
            if not arr
        ]}
        for c in contexts
    ]
    empties = [e for e in empty_arrays if e["empty"]]
    if empties:
        warnings.append(
            {
                "kind": "empty_arrays",
                "message": f"{len(empties)} 个订单含空数组；空数组不产生行，但订单仍在订单粒度可见",
                "orders": empties[:10],
            }
        )
    dups = []
    for ctx in contexts:
        codes: dict[str, int] = {}
        for d in ctx.discounts:
            code = d["payload"].get("code", "")
            codes[code] = codes.get(code, 0) + 1
        for code, n in codes.items():
            if n > 1:
                dups.append({"order_key": ctx.order_key, "code": code, "positions": n})
    if dups:
        warnings.append(
            {
                "kind": "duplicate_discount_code",
                "message": "优惠码重复出现是真实重复：逐元素计数，未按 code 去重",
                "items": dups,
            }
        )
    edge_mismatch = []
    for ctx in contexts:
        expected = max(1, len(ctx.items)) * max(1, len(ctx.discounts)) * max(1, len(ctx.fulfillments))
        if ctx.edge_count != expected:
            edge_mismatch.append({"order_key": ctx.order_key, "edges": ctx.edge_count, "expected": expected})
    if edge_mismatch:
        warnings.append(
            {
                "kind": "bridge_edge_mismatch",
                "message": "关联桥边数与数组规模不一致，结果可能不可靠",
                "orders": edge_mismatch,
            }
        )
    if filters:
        warnings.append(
            {
                "kind": "filtered_view",
                "message": "过滤只筛选输出行；优惠/运费分摊始终基于整单计算，过滤后小计不等于订单总额是预期行为",
            }
        )
    return warnings


def metric_reference() -> dict:
    """Explain, for every grain, which metrics may be summed and why."""
    out = {}
    for grain in GRAIN_KEYS:
        entries = []
        for metric, spec in METRICS.items():
            ok, reason = _metric_allowed(metric, grain)
            entries.append(
                {
                    "metric": metric,
                    "label": spec["label"],
                    "kind": spec["kind"],
                    "accepted": ok,
                    "reason": reason,
                    "additive_at": spec["additive_at"],
                    "native": spec["native"],
                }
            )
        out[grain] = {
            "label": GRAIN_LABEL[grain],
            "native_metrics": metrics_for_grain(grain),
            "metrics": entries,
        }
    return out
