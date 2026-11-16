"""Query plan shape and dataset/plan digests.

A plan pins three things that must move together:

* ``order_version`` - the latest committed version of the order document,
* ``projection``    - the bridge projection generation (structural upgrades
  bump it), and
* ``plan_hash``     - a digest of the order state + query recipe.

The *dataset digest* hashes every order's pinned tuple.  A result may only be
published while its dataset digest still equals the store's current one, so a
result computed before an incremental correction or structural upgrade can
never overwrite a result computed after it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .schema import GRAIN_KEYS


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    order_key: str
    order_version: int
    projection: int
    grain: str
    metrics: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    filter_: dict[str, Any] = field(default_factory=dict)
    shape: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "order_key": self.order_key,
            "order_version": self.order_version,
            "projection": self.projection,
            "grain": self.grain,
            "metrics": list(self.metrics),
            "group_by": list(self.group_by),
            "filter": dict(self.filter_),
            "shape": dict(self.shape),
        }


def build_plan(
    *,
    order_key: str,
    order_version: int,
    projection: int,
    grain: str = "order",
    metrics: Iterable[str] = (),
    group_by: Iterable[str] = (),
    filter_: dict[str, Any] | None = None,
    shape: dict[str, int] | None = None,
) -> Plan:
    if grain not in GRAIN_KEYS:
        raise PlanError(f"unknown grain {grain!r}; expected one of {', '.join(GRAIN_KEYS)}")
    return Plan(
        order_key=order_key,
        order_version=order_version,
        projection=projection,
        grain=grain,
        metrics=tuple(metrics),
        group_by=tuple(group_by),
        filter_=filter_ or {},
        shape=dict(shape or {}),
    )


def _hash_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def plan_digest(plan: Plan) -> str:
    return _hash_json(plan.as_dict())


def dataset_digest(states: Iterable[tuple[str, int, int, str]]) -> str:
    """Hash the pinned state of every order.

    ``states`` items are ``(order_key, version, projection, plan_hash)``.
    """
    h = hashlib.sha256()
    for order_key, version, projection, plan_hash in sorted(states):
        h.update(f"{order_key}|{version}|{projection}|{plan_hash}\n".encode("utf-8"))
    return h.hexdigest()


def query_digest(spec: dict) -> str:
    return _hash_json(spec)
