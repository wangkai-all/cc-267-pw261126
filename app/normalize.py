"""Normalization of raw, versioned order documents into canonical records.

Raw documents arrive as JSON objects.  Three source schemas exist:

* ``orders.v1`` - flat arrays with string cents, no nested shipping lines.
* ``orders.v2`` - nested ``shipping`` object, discount percentages.
* ``orders.v3`` - the current shape, integer cents, discount ``amount_cents``.

Normalization is deliberately strict: unknown top-level arrays are an error
(not silently ignored), missing element ids are filled with a positional
synthetic id, and every discount element keeps a unique ``element_key`` even
when its business id repeats.  Those synthetic keys are what makes the later
"do not dedupe real duplicates" rule enforceable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .schema import SUPPORTED_SOURCE_SCHEMAS


class NormalizationError(ValueError):
    """A document cannot be mapped onto the canonical schema."""


def _as_int(value: Any, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise NormalizationError(f"{field_name}: bool is not a valid number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise NormalizationError(f"{field_name}: fractional cents not supported")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(Decimal(text))
        except (InvalidOperation, ValueError) as exc:
            raise NormalizationError(f"{field_name}: {value!r} is not a number") from exc
    raise NormalizationError(f"{field_name}: unsupported type {type(value).__name__}")


def _as_cent(value: Any, field_name: str) -> int:
    """Parse money into integer cents.

    Integers are taken as cents.  Strings with a decimal point / currency symbol
    are parsed as yuan and converted.  Floats are parsed as yuan too.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise NormalizationError(f"{field_name}: bool is not money")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(Decimal(str(value)).scaleb(2))
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("¥", "").replace("￥", "")
        if not text:
            return 0
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise NormalizationError(f"{field_name}: {value!r} is not money") from exc
        if dec == dec.to_integral_value():
            # An integer string in source v1 means cents.
            return int(dec)
        return int(dec.scaleb(2))
    raise NormalizationError(f"{field_name}: unsupported money type {type(value).__name__}")


def _as_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise NormalizationError(f"{field_name}: bad timestamp {value!r}") from exc
    else:
        raise NormalizationError(f"{field_name}: missing timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_str(value: Any, field_name: str, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        raise NormalizationError(f"{field_name}: expected scalar")
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint_document(doc: dict) -> str:
    """Hash the *business content* of a document, ignoring bookkeeping keys."""
    volatile = {"as_of", "_meta", "ingested_at", "request_id", "trace_id"}
    cleaned = {k: v for k, v in doc.items() if k not in volatile}
    return hashlib.sha256(canonical_json(cleaned).encode("utf-8")).hexdigest()


@dataclass
class RawElement:
    array: str
    ord: int
    business_id: str
    payload: dict


@dataclass
class CanonicalOrder:
    order_key: str
    doc: dict
    schema_version: str
    order_id: str
    tenant: str
    channel: str
    status: str
    placed_at: datetime
    currency: str
    order_amount: int
    items: list[RawElement] = field(default_factory=list)
    discounts: list[RawElement] = field(default_factory=list)
    fulfillments: list[RawElement] = field(default_factory=list)

    @property
    def arrays(self) -> dict[str, list[RawElement]]:
        return {"items": self.items, "discounts": self.discounts, "fulfillments": self.fulfillments}


def _read_nested(obj: dict, path: str, default: Any = None) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def normalize_document(raw: dict, source_version: str | None = None) -> CanonicalOrder:
    if not isinstance(raw, dict):
        raise NormalizationError("document must be a JSON object")
    version = _as_str(raw.get("schema_version") or source_version, "schema_version")
    if not version:
        raise NormalizationError("schema_version is required")
    if version not in SUPPORTED_SOURCE_SCHEMAS:
        raise NormalizationError(
            f"unsupported schema_version {version!r}; supported: {', '.join(SUPPORTED_SOURCE_SCHEMAS)}"
        )

    order_id = _as_str(raw.get("order_id") or raw.get("id"), "order_id")
    if not order_id:
        raise NormalizationError("order_id is required")
    tenant = _as_str(raw.get("tenant") or raw.get("shop"), "tenant", default="default")
    channel = _as_str(raw.get("channel"), "channel", default="web")
    status = _as_str(raw.get("status") or raw.get("state"), "status", default="paid")
    currency = _as_str(raw.get("currency"), "currency", default="CNY")
    placed_at = _as_time(raw.get("placed_at") or raw.get("created_at"), "placed_at")

    if version == "orders.v1":
        amount_raw = raw.get("order_amount", raw.get("total", 0))
        order_amount = _as_cent(amount_raw, "order_amount") if isinstance(amount_raw, str) else _as_int(amount_raw, "order_amount")
    elif version == "orders.v2":
        amount_raw = raw.get("order_amount", raw.get("amount", 0))
        order_amount = _as_cent(amount_raw, "order_amount") if isinstance(amount_raw, str) else _as_int(amount_raw, "order_amount")
    else:
        order_amount = _as_int(raw.get("order_amount"), "order_amount")

    for key in ("items", "discounts", "fulfillments"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], list):
            raise NormalizationError(f"{key}: expected an array")

    items = _normalize_items(version, raw.get("items") or [])
    discounts = _normalize_discounts(version, raw.get("discounts") or [])
    fulfillments = _normalize_fulfillments(version, raw.get("fulfillments") or [])

    order_key = f"{tenant}/{order_id}"
    return CanonicalOrder(
        order_key=order_key,
        doc=raw,
        schema_version=version,
        order_id=order_id,
        tenant=tenant,
        channel=channel,
        status=status,
        placed_at=placed_at,
        currency=currency,
        order_amount=order_amount,
        items=items,
        discounts=discounts,
        fulfillments=fulfillments,
    )


def _normalize_items(version: str, raw_items: list) -> list[RawElement]:
    out: list[RawElement] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise NormalizationError(f"items[{idx}]: expected object")
        business_id = _as_str(item.get("item_id") or item.get("sku"), f"items[{idx}].item_id")
        if not business_id:
            business_id = f"__pos_{idx}"
        sku = _as_str(item.get("sku"), f"items[{idx}].sku", default=business_id)
        title = _as_str(item.get("title") or item.get("name"), f"items[{idx}].title", default=sku)
        qty = _as_int(item.get("qty", item.get("quantity", 1)), f"items[{idx}].qty")
        if qty < 0:
            raise NormalizationError(f"items[{idx}].qty: negative quantity")
        unit_raw = item.get("unit_price_cents", item.get("unit_price", item.get("price", 0)))
        if version == "orders.v1":
            unit_price = _as_cent(unit_raw, f"items[{idx}].unit_price")
        elif version == "orders.v2":
            unit_price = _as_cent(unit_raw, f"items[{idx}].unit_price")
        else:
            unit_price = _as_int(item.get("unit_price_cents", unit_raw), f"items[{idx}].unit_price_cents")
        payload = {
            "sku": sku,
            "title": title,
            "qty": qty,
            "unit_price_cents": unit_price,
            "position": f"$.items[{idx}]",
            "element_index": idx,
            "source_item_id": business_id,
        }
        out.append(RawElement("items", idx, business_id, payload))
    return out


def _normalize_discounts(version: str, raw_discounts: list) -> list[RawElement]:
    out: list[RawElement] = []
    for idx, disc in enumerate(raw_discounts):
        if not isinstance(disc, dict):
            raise NormalizationError(f"discounts[{idx}]: expected object")
        code = _as_str(disc.get("code") or disc.get("coupon_code"), f"discounts[{idx}].code", default="")
        business_id = _as_str(disc.get("discount_id"), f"discounts[{idx}].discount_id")
        if not business_id:
            business_id = f"__pos_{idx}"
        kind = _as_str(disc.get("kind") or disc.get("type"), f"discounts[{idx}].kind", default="promotion")
        if version == "orders.v2" and "percent" in disc:
            gross_hint = _as_int(disc.get("base_cents"), f"discounts[{idx}].base_cents")
            pct = Decimal(str(disc.get("percent")))
            amount = int((Decimal(gross_hint) * pct / Decimal(100)).to_integral_value())
        else:
            raw_amount = disc.get("amount_cents", disc.get("amount", 0))
            amount = _as_int(raw_amount, f"discounts[{idx}].amount_cents") if version == "orders.v3" else _as_cent(raw_amount, f"discounts[{idx}].amount")
        if amount < 0:
            raise NormalizationError(f"discounts[{idx}].amount_cents: negative")
        payload = {
            "code": code,
            "kind": kind,
            "amount_cents": amount,
            "position": f"$.discounts[{idx}]",
            "element_index": idx,
            "source_discount_id": business_id,
        }
        out.append(RawElement("discounts", idx, business_id, payload))
    return out


def _normalize_fulfillments(version: str, raw_fulfillments: list) -> list[RawElement]:
    out: list[RawElement] = []
    for idx, ful in enumerate(raw_fulfillments):
        if not isinstance(ful, dict):
            raise NormalizationError(f"fulfillments[{idx}]: expected object")
        business_id = _as_str(ful.get("fulfillment_id"), f"fulfillments[{idx}].fulfillment_id")
        if not business_id:
            business_id = _as_str(ful.get("tracking_no"), f"fulfillments[{idx}].fulfillment_id", default="")
        if not business_id:
            business_id = f"__pos_{idx}"
        carrier = _as_str(ful.get("carrier"), f"fulfillments[{idx}].carrier", default="unknown")
        tracking = _as_str(ful.get("tracking_no"), f"fulfillments[{idx}].tracking_no", default="")
        lines = ful.get("lines") or ful.get("items") or []
        if not isinstance(lines, list):
            raise NormalizationError(f"fulfillments[{idx}].lines: expected array")
        line_count = len(lines)
        qty = 0
        for li, line in enumerate(lines):
            if not isinstance(line, dict):
                raise NormalizationError(f"fulfillments[{idx}].lines[{li}]: expected object")
            qty += _as_int(line.get("qty", 1), f"fulfillments[{idx}].lines[{li}].qty")

        if version == "orders.v1":
            shipping_raw = ful.get("shipping_fee", 0)
            shipping_fee = _as_cent(shipping_raw, f"fulfillments[{idx}].shipping_fee")
        elif version == "orders.v2":
            shipping_raw = _read_nested(ful, "shipping.fee", 0)
            shipping_fee = _as_cent(shipping_raw, f"fulfillments[{idx}].shipping.fee")
        else:
            shipping_fee = _as_int(ful.get("shipping_fee_cents"), f"fulfillments[{idx}].shipping_fee_cents")
        if shipping_fee < 0:
            raise NormalizationError(f"fulfillments[{idx}].shipping fee: negative")
        if line_count == 0 and qty == 0:
            qty = _as_int(ful.get("qty", 0), f"fulfillments[{idx}].qty") or 0
        payload = {
            "carrier": carrier,
            "tracking_no": tracking,
            "shipping_fee_cents": shipping_fee,
            "qty": qty,
            "line_count": line_count,
            "position": f"$.fulfillments[{idx}]",
            "element_index": idx,
            "source_fulfillment_id": business_id,
        }
        out.append(RawElement("fulfillments", idx, business_id, payload))
    return out


def dumps_order(order: CanonicalOrder) -> dict:
    """Canonical on-disk representation used by the store fixtures."""
    def el(e: RawElement, id_field: str) -> dict:
        return {"id": e.business_id, "ord": e.ord, **e.payload}

    return {
        "schema_version": order.schema_version,
        "order_key": order.order_key,
        "order_id": order.order_id,
        "tenant": order.tenant,
        "channel": order.channel,
        "status": order.status,
        "placed_at": order.placed_at.isoformat(),
        "currency": order.currency,
        "order_amount_cents": order.order_amount,
        "items": [el(e, "item_id") for e in order.items],
        "discounts": [el(e, "discount_id") for e in order.discounts],
        "fulfillments": [el(e, "fulfillment_id") for e in order.fulfillments],
    }


def iter_documents(payload: Iterable[dict]) -> Iterable[CanonicalOrder]:
    for idx, raw in enumerate(payload):
        try:
            yield normalize_document(raw)
        except NormalizationError as exc:
            raise NormalizationError(f"document #{idx} ({raw.get('order_id', '?')}): {exc}") from exc
