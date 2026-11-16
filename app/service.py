"""Application service: one place that opens the store and exposes the
operations used by both the CLI and the HTTP UI.

Keeping this separate matters for the demo scripts: the web layer, the CLI and
the tests all drive the same functions, so a number shown in the browser is a
number the tests can reproduce.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .executor import metric_reference, run_query
from .metrics import catalogue
from .schema import GRAIN_KEYS, GRAIN_LABEL, IDENTITY_COLUMNS
from .store import LoadResult, Store

DEFAULT_DB = os.path.join("data", "orders.sqlite3")
FIXTURE = os.path.join("data", "orders.json")
CORRECTIONS = os.path.join("data", "corrections.json")


def default_db() -> str:
    # Read the environment at call time so tests and the CLI can point a
    # process at a different database without re-importing the module.
    return os.environ.get("ORDERS_DB", DEFAULT_DB)


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Service:
    store: Store

    @classmethod
    def open(cls, path: str | None = None) -> "Service":
        return cls(Store(path or default_db()))

    # ------------------------------------------------------------- fixtures
    def seed_if_empty(self, fixture: str = FIXTURE) -> dict | None:
        if self.store.stats()["orders"] > 0:
            return None
        return self.load(fixture)

    def load(self, fixture: str = FIXTURE, **kwargs) -> dict:
        docs = read_json(fixture)
        result = self.store.load(os.path.basename(fixture), docs, **kwargs)
        return _load_dict(result)

    def load_corrections(self, fixture: str = CORRECTIONS) -> dict:
        docs = read_json(fixture)
        result = self.store.load(os.path.basename(fixture), docs)
        return _load_dict(result)

    def path_for(self, fixture: str) -> str:
        return CORRECTIONS if fixture == "corrections" else FIXTURE

    def upgrade(self) -> dict:
        return self.store.structural_upgrade()

    def audit(self) -> dict:
        return self.store.audit()

    def resume_demo(self, fixture: str = FIXTURE, interrupt_after: int = 4) -> dict:
        """Interrupt a load mid-stream, then resume it from its checkpoints.

        Runs in one call so the console can show both run rows and the
        exactly-once outcome without holding a run id in the browser.
        """
        docs = read_json(fixture)
        interrupted_run = None
        try:
            self.store.load(
                os.path.basename(fixture),
                docs,
                interrupt_after=interrupt_after,
                note="manual resume demo: interrupted",
            )
        except KeyboardInterrupt:
            runs = [dict(r) for r in self.store.load_runs()]
            interrupted_run = next(r for r in reversed(runs) if r["status"] == "aborted")
        if interrupted_run is None:
            raise RuntimeError("load was not interrupted; the document stream may be shorter than interrupt_after")
        resumed = self.store.load(
            os.path.basename(fixture),
            docs,
            resume_of=interrupted_run["run_id"],
            note="manual resume demo: resumed",
        )
        checkpoints = [
            dict(r) for r in self.store.checkpoints(interrupted_run["run_id"])
        ]
        return {
            "interrupted": interrupted_run,
            "committed_before_interrupt": len(checkpoints),
            "checkpoints": checkpoints,
            "resumed": _load_dict(resumed),
            "audit": self.store.audit(),
        }

    # --------------------------------------------------------------- query
    def query(
        self,
        grain: str,
        metrics: list[str],
        group_by: list[str] | None = None,
        filters: dict | None = None,
        *,
        use_cache: bool = True,
        publish: bool = True,
        detail: bool = False,
    ) -> dict:
        return run_query(
            self.store,
            grain=grain,
            metrics=metrics,
            group_by=group_by or [],
            filters=filters or {},
            use_cache=use_cache,
            publish=publish,
            detail=detail,
        )

    # --------------------------------------------------------------- views
    def bootstrap(self) -> dict:
        stats = self.store.stats()
        return {
            "ok": True,
            "catalogue": catalogue(),
            "metric_reference": metric_reference(),
            "grains": [
                {"name": g, "label": GRAIN_LABEL[g], "identity_columns": IDENTITY_COLUMNS[g]}
                for g in GRAIN_KEYS
            ],
            "stats": stats,
            "orders": [
                {
                    "order_key": s["order_key"],
                    "order_id": s["order_id"],
                    "tenant": s["tenant"],
                    "channel": s["channel"],
                    "status": s["status"],
                    "placed_at": s["placed_at"],
                    "placed_date": s["placed_date"],
                    "order_amount_cents": s["order_amount_cents"],
                    "version": s["version"],
                    "projection": s["projection"],
                    "schema_version": s["schema_version"],
                    "shape": s["shape"],
                    "plan_hash": s["plan_hash"],
                }
                for s in self.store.latest_states()
            ],
            "load_runs": [dict(r) for r in self.store.load_runs()],
            "cache": self.store.cache_history(),
            "fixtures": {
                "base": os.path.basename(FIXTURE),
                "corrections": os.path.basename(CORRECTIONS),
            },
        }

    def order_detail(self, order_key: str) -> dict:
        state = next((s for s in self.store.latest_states() if s["order_key"] == order_key), None)
        if state is None:
            raise KeyError(order_key)
        version = int(state["version"])
        rows = self.store.rows_for(order_key, version)
        plan = self.store.plan_for(order_key)
        history = self.store.plan_history(order_key)
        return {
            "ok": True,
            "state": state,
            "document": json.loads(
                self.store.order_document(order_key, version)["document"]
            ),
            "bridge_rows": [
                {
                    "grain": r["grain"],
                    "element_ord": r["element_ord"],
                    "source_id": r["source_id"],
                    "array_size": r["array_size"],
                    "payload": json.loads(r["payload"]),
                }
                for r in rows
            ],
            "plan": {
                "order_version": plan["order_version"],
                "projection": plan["projection"],
                "plan_hash": plan["plan_hash"],
                "shape": json.loads(plan["shape_json"]),
                "plan": json.loads(plan["plan_json"]),
            },
            "plan_history": [
                {
                    "order_version": h["order_version"],
                    "projection": h["projection"],
                    "plan_hash": h["plan_hash"],
                    "reason": h["reason"],
                    "created_at": h["created_at"],
                }
                for h in history
            ],
            "edges": self.store.edge_count(order_key, version),
        }

    def ops_state(self) -> dict:
        return {
            "stats": self.store.stats(),
            "load_runs": [dict(r) for r in self.store.load_runs()],
            "cache": self.store.cache_history(),
        }


def _load_dict(result: LoadResult) -> dict:
    return {
        "run_id": result.run_id,
        "processed": result.processed,
        "resumed_from": result.resumed_from,
        "written": result.written,
        "unchanged": result.unchanged,
        "upgraded": result.upgraded,
        "status": result.status,
        "documents": result.documents,
    }
