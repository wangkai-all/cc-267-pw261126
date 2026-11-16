"""Versioned nested-order store.

Concurrency model
-----------------
Every operation uses its own SQLite connection.  Reads run inside a deferred
transaction and therefore see one consistent snapshot; writes run inside
``BEGIN IMMEDIATE`` and are serialised by SQLite, with ``busy_timeout`` to
queue rather than fail.  This is what makes an incremental correction, a
structural upgrade and a query/cache publication safe to run at the same
time:

* the projection generation is read *inside* each write transaction, so a
  document loaded after an upgrade is written under the new projection and a
  document loaded before it is re-projected by the upgrade itself;
* a query pins ``(order_version, projection, plan_hash)`` for every order and
  a *dataset digest* of that pinned state;
* publication is compare-and-swap on the dataset digest, so a result computed
  before any of those changes is refused instead of overwriting the newer one;
* cache rows are keyed by ``(query_key, dataset_digest)`` and are append-only;
  ``result_head`` names the authoritative entry for a query.

Storage invariants
------------------
* Order documents are immutable.  Corrections append version N+1.
* Bridge rows are keyed by element position ``(order_key, version, grain,
  element_ord)``.  ``source_id`` is metadata: repeated discount codes and
  repeated tracking numbers stay as separate rows.
* Each order has a plan pinned to version + projection + array shape, so the
  plan hash changes on both correction and upgrade.
* Loads commit one document plus its checkpoint per transaction.  A crash
  leaves committed checkpoints; a resumed run replays the same stream,
  verifies each committed document's fingerprint, skips that prefix and
  continues.  The resulting state is byte-identical to an uninterrupted load.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterator

from .normalize import (
    CanonicalOrder,
    canonical_json,
    fingerprint_document,
    normalize_document,
)
from .plan import Plan, build_plan, dataset_digest, plan_digest
from .schema import BRIDGE_SCHEMA_VERSION

STORE_LOCK = threading.RLock()


class StoreError(RuntimeError):
    pass


class VersionConflict(StoreError):
    pass


class StaleResult(StoreError):
    """A stale computation was refused publication."""


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS load_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    resumed_from INTEGER NOT NULL DEFAULT 0,
    resume_of TEXT,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS load_checkpoints (
    run_id TEXT NOT NULL,
    doc_index INTEGER NOT NULL,
    order_key TEXT NOT NULL,
    order_version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (run_id, doc_index)
);

CREATE TABLE IF NOT EXISTS order_versions (
    order_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    projection INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    order_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    placed_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    order_amount_cents INTEGER NOT NULL,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_key, version)
);

CREATE TABLE IF NOT EXISTS bridge_rows (
    order_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    grain TEXT NOT NULL,
    element_ord INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    array_size INTEGER NOT NULL,
    payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_key, version, grain, element_ord)
);

CREATE INDEX IF NOT EXISTS bridge_by_grain
    ON bridge_rows (grain, order_key, version, element_ord);

CREATE TABLE IF NOT EXISTS bridge_edges (
    order_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    item_ord INTEGER NOT NULL,
    discount_ord INTEGER NOT NULL,
    fulfillment_ord INTEGER NOT NULL,
    PRIMARY KEY (order_key, version, item_ord, discount_ord, fulfillment_ord)
);

CREATE TABLE IF NOT EXISTS order_plan (
    order_key TEXT PRIMARY KEY,
    order_version INTEGER NOT NULL,
    projection INTEGER NOT NULL,
    shape_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_history (
    order_key TEXT NOT NULL,
    order_version INTEGER NOT NULL,
    projection INTEGER NOT NULL,
    plan_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_key, order_version, projection)
);

CREATE TABLE IF NOT EXISTS result_cache (
    query_key TEXT NOT NULL,
    dataset_digest TEXT NOT NULL,
    grain TEXT NOT NULL,
    plan_set TEXT NOT NULL,
    payload TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (query_key, dataset_digest)
);

CREATE TABLE IF NOT EXISTS result_head (
    query_key TEXT PRIMARY KEY,
    dataset_digest TEXT NOT NULL,
    grain TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00", time.gmtime())


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@dataclass
class LoadResult:
    run_id: str
    processed: int
    resumed_from: int
    written: int
    unchanged: int
    upgraded: int
    status: str
    documents: int


# ---------------------------------------------------------------------------
# Read snapshot
# ---------------------------------------------------------------------------


class Snapshot:
    """Read-only view bound to one connection / one transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.closed = False

    def _get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def projection(self) -> int:
        return int(self._get_meta("projection", "0") or "0")

    def schema_version(self) -> str:
        return self._get_meta("schema_version", BRIDGE_SCHEMA_VERSION) or BRIDGE_SCHEMA_VERSION

    def latest_states(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ov.order_key, ov.version, ov.projection, ov.schema_version, ov.fingerprint, "
            "ov.order_id, ov.tenant, ov.channel, ov.status, ov.placed_at, ov.placed_date, ov.currency, "
            "ov.order_amount_cents, op.plan_hash, op.plan_json, op.shape_json "
            "FROM order_versions ov JOIN "
            "(SELECT order_key, MAX(version) AS v FROM order_versions GROUP BY order_key) l "
            "ON ov.order_key=l.order_key AND ov.version=l.v "
            "LEFT JOIN order_plan op ON op.order_key=ov.order_key "
            "ORDER BY ov.order_key"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["shape"] = json.loads(d.pop("shape_json") or "{}")
            d.pop("plan_json", None)
            d["version"] = int(d["version"])
            d["projection"] = int(d["projection"])
            out.append(d)
        return out

    def current_dataset_digest(self) -> str:
        return dataset_digest(
            (s["order_key"], int(s["version"]), int(s["projection"]), s["plan_hash"] or "")
            for s in self.latest_states()
        )

    def rows_for(self, order_key: str, version: int, grain: str | None = None) -> list[sqlite3.Row]:
        if grain is None:
            return self.conn.execute(
                "SELECT * FROM bridge_rows WHERE order_key=? AND version=? "
                "ORDER BY CASE grain WHEN 'order' THEN 0 WHEN 'item' THEN 1 "
                "WHEN 'discount' THEN 2 ELSE 3 END, element_ord",
                (order_key, version),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM bridge_rows WHERE order_key=? AND version=? AND grain=? ORDER BY element_ord",
            (order_key, version, grain),
        ).fetchall()

    def edge_count(self, order_key: str, version: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_edges WHERE order_key=? AND version=?",
            (order_key, version),
        ).fetchone()
        return int(row["n"])

    def plan_for(self, order_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM order_plan WHERE order_key=?", (order_key,)).fetchone()

    def plan_history(self, order_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM plan_history WHERE order_key=? ORDER BY order_version, projection",
            (order_key,),
        ).fetchall()

    def order_document(self, order_key: str, version: int | None = None) -> sqlite3.Row | None:
        if version is None:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM order_versions WHERE order_key=?", (order_key,)
            ).fetchone()
            version = int(row["v"]) if row and row["v"] is not None else None
        if version is None:
            return None
        return self.conn.execute(
            "SELECT * FROM order_versions WHERE order_key=? AND version=?", (order_key, version)
        ).fetchone()

    def cached_result(self, query_key: str, dataset_digest_value: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM result_cache WHERE query_key=? AND dataset_digest=?",
            (query_key, dataset_digest_value),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def cache_history(self, query_key: str | None = None) -> list[dict]:
        if query_key:
            rows = self.conn.execute(
                "SELECT rc.*, rh.dataset_digest AS head_digest FROM result_cache rc "
                "LEFT JOIN result_head rh ON rh.query_key=rc.query_key "
                "WHERE rc.query_key=? ORDER BY rc.computed_at",
                (query_key,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT rc.*, rh.dataset_digest AS head_digest FROM result_cache rc "
                "LEFT JOIN result_head rh ON rh.query_key=rc.query_key "
                "ORDER BY rc.computed_at"
            ).fetchall()
        return [
            {
                "query_key": r["query_key"],
                "dataset_digest": r["dataset_digest"],
                "grain": r["grain"],
                "is_head": r["dataset_digest"] == r["head_digest"],
                "computed_at": r["computed_at"],
            }
            for r in rows
        ]

    def result_head(self, query_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM result_head WHERE query_key=?", (query_key,)).fetchone()

    def load_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM load_runs ORDER BY started_at, run_id").fetchall()

    def stats(self) -> dict:
        n_bridge = self.conn.execute("SELECT COUNT(*) AS n FROM bridge_rows").fetchone()["n"]
        n_orders = self.conn.execute("SELECT COUNT(DISTINCT order_key) AS n FROM order_versions").fetchone()["n"]
        n_versions = self.conn.execute("SELECT COUNT(*) AS n FROM order_versions").fetchone()["n"]
        n_cache = self.conn.execute("SELECT COUNT(*) AS n FROM result_cache").fetchone()["n"]
        return {
            "orders": int(n_orders),
            "versions": int(n_versions),
            "bridge_rows": int(n_bridge),
            "cache_entries": int(n_cache),
            "projection": self.projection(),
            "schema_version": self.schema_version(),
            "dataset_digest": self.current_dataset_digest(),
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, path: str):
        self.path = path
        with STORE_LOCK:
            conn = _connect(path)
            try:
                conn.executescript(DDL)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (BRIDGE_SCHEMA_VERSION,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('projection', '0')"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (utcnow(),)
                )
            finally:
                conn.close()

    # ------------------------------------------------------------- plumbing
    @contextlib.contextmanager
    def snapshot(self) -> Iterator[Snapshot]:
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN")
            snap = Snapshot(conn)
            try:
                yield snap
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()

    def close(self) -> None:
        """No persistent connection is held; kept for API symmetry."""

    # ------------------------------------------------------------ snapshots
    def latest_states(self) -> list[dict]:
        with self.snapshot() as snap:
            return snap.latest_states()

    def current_dataset_digest(self) -> str:
        with self.snapshot() as snap:
            return snap.current_dataset_digest()

    def rows_for(self, order_key: str, version: int, grain: str | None = None) -> list[sqlite3.Row]:
        with self.snapshot() as snap:
            return snap.rows_for(order_key, version, grain)

    def edge_count(self, order_key: str, version: int) -> int:
        with self.snapshot() as snap:
            return snap.edge_count(order_key, version)

    def plan_for(self, order_key: str) -> sqlite3.Row | None:
        with self.snapshot() as snap:
            return snap.plan_for(order_key)

    def plan_history(self, order_key: str) -> list[sqlite3.Row]:
        with self.snapshot() as snap:
            return snap.plan_history(order_key)

    def order_document(self, order_key: str, version: int | None = None) -> sqlite3.Row | None:
        with self.snapshot() as snap:
            return snap.order_document(order_key, version)

    def load_runs(self) -> list[sqlite3.Row]:
        with self.snapshot() as snap:
            return snap.load_runs()

    def cache_history(self, query_key: str | None = None) -> list[dict]:
        with self.snapshot() as snap:
            return snap.cache_history(query_key)

    def cached_result(self, query_key: str, digest: str) -> dict | None:
        with self.snapshot() as snap:
            return snap.cached_result(query_key, digest)

    def stats(self) -> dict:
        with self.snapshot() as snap:
            return snap.stats()

    def latest_order_version(self, order_key: str) -> int:
        with self.snapshot() as snap:
            row = snap.conn.execute(
                "SELECT MAX(version) AS v FROM order_versions WHERE order_key=?", (order_key,)
            ).fetchone()
            return int(row["v"]) if row and row["v"] is not None else 0

    def checkpoints(self, run_id: str) -> list[sqlite3.Row]:
        with self.snapshot() as snap:
            return snap.conn.execute(
                "SELECT * FROM load_checkpoints WHERE run_id=? ORDER BY doc_index", (run_id,)
            ).fetchall()

    # ------------------------------------------------------------------ load
    def _next_run_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM load_runs").fetchone()
        return int(row["n"]) + 1

    def _new_run_id(self, conn: sqlite3.Connection) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        return f"run-{stamp}-{self._next_run_seq(conn)}"

    def load(
        self,
        source: str,
        documents: list[dict],
        *,
        interrupt_after: int | None = None,
        resume_of: str | None = None,
        note: str = "",
    ) -> LoadResult:
        prior = self.checkpoints(resume_of) if resume_of else []
        if prior and len(prior) > len(documents):
            raise VersionConflict(
                f"resume stream has {len(documents)} documents but run {resume_of} committed {len(prior)}"
            )
        with self._write() as conn:
            run_id = self._new_run_id(conn)
            conn.execute(
                "INSERT INTO load_runs(run_id, source, started_at, status, resume_of, resumed_from, note) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, source, utcnow(), "running", resume_of, len(prior), note),
            )

        written = unchanged = upgraded = 0
        processed = 0
        try:
            for idx, raw in enumerate(documents):
                if idx < len(prior):
                    cp = prior[idx]
                    norm = normalize_document(raw)
                    if norm.order_key != cp["order_key"]:
                        raise VersionConflict(
                            f"resume mismatch at document {idx}: checkpoint has {cp['order_key']}, "
                            f"stream has {norm.order_key}"
                        )
                    if fingerprint_document(norm.doc) != cp["fingerprint"]:
                        raise VersionConflict(
                            f"resume mismatch at document {idx} ({norm.order_key}): "
                            "document content changed since the committed checkpoint"
                        )
                    with self._write() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO load_checkpoints"
                            "(run_id, doc_index, order_key, order_version, fingerprint) VALUES(?,?,?,?,?)",
                            (run_id, idx, cp["order_key"], cp["order_version"], cp["fingerprint"]),
                        )
                    processed += 1
                    continue

                if interrupt_after is not None and idx >= interrupt_after:
                    raise KeyboardInterrupt(f"simulated interruption before document {idx}")

                norm = normalize_document(raw)
                with self._write() as conn:
                    projection = _meta_int(conn, "projection")
                    outcome = _apply_document(conn, norm, projection)
                    conn.execute(
                        "INSERT INTO load_checkpoints"
                        "(run_id, doc_index, order_key, order_version, fingerprint) VALUES(?,?,?,?,?)",
                        (run_id, idx, norm.order_key, outcome["version"], outcome["fingerprint"]),
                    )
                processed += 1
                written += 1 if outcome["written"] else 0
                unchanged += 0 if outcome["written"] else 1
                upgraded += 1 if outcome["upgraded"] else 0
        except KeyboardInterrupt:
            with self._write() as conn:
                conn.execute(
                    "UPDATE load_runs SET status='aborted', finished_at=?, note=?, processed=? WHERE run_id=?",
                    (
                        utcnow(),
                        f"interrupted after {interrupt_after} of {len(documents)} documents",
                        processed,
                        run_id,
                    ),
                )
            raise
        except Exception as exc:
            with self._write() as conn:
                conn.execute(
                    "UPDATE load_runs SET status='failed', finished_at=?, note=?, processed=? WHERE run_id=?",
                    (utcnow(), f"{type(exc).__name__}: {exc}"[:500], processed, run_id),
                )
            raise

        with self._write() as conn:
            conn.execute(
                "UPDATE load_runs SET status='committed', finished_at=?, note=?, processed=? WHERE run_id=?",
                (
                    utcnow(),
                    note or f"{written} written, {unchanged} unchanged, {upgraded} upgraded",
                    processed,
                    run_id,
                ),
            )
        return LoadResult(
            run_id=run_id,
            processed=processed,
            resumed_from=len(prior),
            written=written,
            unchanged=unchanged,
            upgraded=upgraded,
            status="committed",
            documents=len(documents),
        )

    # ------------------------------------------------------------- upgrade
    def structural_upgrade(self) -> dict:
        """Re-project every latest order under the current normalizer.

        One write transaction: bump projection, re-project, rewrite plans.  A
        concurrent load either commits before (and is re-projected here) or
        after (and reads the new projection inside its own transaction).
        """
        with self._write() as conn:
            old_projection = _meta_int(conn, "projection")
            new_projection = old_projection + 1
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('projection', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_projection),),
            )
            latest = conn.execute(
                "SELECT ov.* FROM order_versions ov JOIN "
                "(SELECT order_key, MAX(version) AS v FROM order_versions GROUP BY order_key) l "
                "ON ov.order_key=l.order_key AND ov.version=l.v"
            ).fetchall()
            now = utcnow()
            for row in latest:
                raw = json.loads(row["document"])
                norm = normalize_document(raw, source_version=row["schema_version"])
                conn.execute(
                    "UPDATE order_versions SET schema_version=?, projection=? "
                    "WHERE order_key=? AND version=?",
                    (norm.schema_version, new_projection, row["order_key"], row["version"]),
                )
                _reproject(conn, norm, row["version"], new_projection, now)
                _write_plan(
                    conn, norm, row["version"], new_projection, "structural upgrade", now
                )
            return {
                "old_projection": old_projection,
                "projection": new_projection,
                "orders": len(latest),
            }

    # ------------------------------------------------------------ publish
    def publish_result(
        self,
        query_key: str,
        *,
        observed_dataset_digest: str,
        grain: str,
        plan_set: list[dict],
        payload: dict,
    ) -> dict:
        """Compare-and-swap publication of a computed result.

        Refuses when the dataset moved since the result was computed.  The
        newer head (grain/version/plan) stays untouched.
        """
        with self._write() as conn:
            snap = Snapshot(conn)
            current = snap.current_dataset_digest()
            if observed_dataset_digest != current:
                raise StaleResult(
                    "result computed on dataset digest "
                    f"{observed_dataset_digest[:12]} but current is {current[:12]}; "
                    "refusing to publish (newer grain/version results stay authoritative)"
                )
            head = conn.execute(
                "SELECT * FROM result_head WHERE query_key=?", (query_key,)
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM result_cache WHERE query_key=? AND dataset_digest=?",
                (query_key, observed_dataset_digest),
            ).fetchone()
            if existing:
                status = "existing"
            else:
                conn.execute(
                    "INSERT INTO result_cache(query_key, dataset_digest, grain, plan_set, payload, computed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        query_key,
                        observed_dataset_digest,
                        grain,
                        canonical_json(plan_set),
                        canonical_json(payload),
                        utcnow(),
                    ),
                )
                status = "published"
            previous = (
                {
                    "dataset_digest": head["dataset_digest"],
                    "grain": head["grain"],
                    "updated_at": head["updated_at"],
                }
                if head
                else None
            )
            conn.execute(
                "INSERT INTO result_head(query_key, dataset_digest, grain, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(query_key) DO UPDATE SET dataset_digest=excluded.dataset_digest, "
                "grain=excluded.grain, updated_at=excluded.updated_at",
                (query_key, observed_dataset_digest, grain, utcnow()),
            )
            return {
                "status": status,
                "dataset_digest": observed_dataset_digest,
                "previous_head": previous,
            }

    # ------------------------------------------------------- consistency audit
    def audit(self) -> dict:
        """Check plan/data/version consistency across the whole store."""
        with self.snapshot() as snap:
            projection = snap.projection()
            states = snap.latest_states()
            problems: list[dict] = []
            checked = 0
            for state in states:
                checked += 1
                order_key = state["order_key"]
                plan = snap.plan_for(order_key)
                if plan is None:
                    problems.append({"order_key": order_key, "problem": "missing plan"})
                    continue
                if int(plan["order_version"]) != int(state["version"]):
                    problems.append(
                        {
                            "order_key": order_key,
                            "problem": "plan order_version != latest version",
                            "plan": plan["order_version"],
                            "data": state["version"],
                        }
                    )
                if int(plan["projection"]) != int(state["projection"]):
                    problems.append(
                        {
                            "order_key": order_key,
                            "problem": "plan projection != data projection",
                            "plan": plan["projection"],
                            "data": state["projection"],
                        }
                    )
                if int(state["projection"]) != projection:
                    problems.append(
                        {
                            "order_key": order_key,
                            "problem": "order projection != store projection",
                            "data": state["projection"],
                            "store": projection,
                        }
                    )
                recomputed = plan_digest(
                    build_plan(
                        order_key=order_key,
                        order_version=int(plan["order_version"]),
                        projection=int(plan["projection"]),
                        grain="order",
                        filter_={"lifecycle": "latest"},
                        shape=json.loads(plan["shape_json"]),
                    )
                )
                if recomputed != plan["plan_hash"]:
                    problems.append({"order_key": order_key, "problem": "plan hash does not match plan"})
                counts = {
                    r["grain"]: int(r["n"])
                    for r in snap.conn.execute(
                        "SELECT grain, COUNT(*) AS n FROM bridge_rows WHERE order_key=? AND version=? "
                        "GROUP BY grain",
                        (order_key, state["version"]),
                    ).fetchall()
                }
                shape = state["shape"]
                for grain, array in (("item", "items"), ("discount", "discounts"), ("fulfillment", "fulfillments")):
                    if counts.get(grain, 0) != int(shape.get(array, 0)):
                        problems.append(
                            {
                                "order_key": order_key,
                                "problem": f"bridge row count != shape for {grain}",
                                "rows": counts.get(grain, 0),
                                "shape": shape.get(array, 0),
                            }
                        )
                expected_edges = (
                    max(1, int(shape.get("items", 0)))
                    * max(1, int(shape.get("discounts", 0)))
                    * max(1, int(shape.get("fulfillments", 0)))
                )
                if snap.edge_count(order_key, state["version"]) != expected_edges:
                    problems.append({"order_key": order_key, "problem": "bridge edge count mismatch"})
            heads = snap.conn.execute("SELECT * FROM result_head").fetchall()
            current_heads = 0
            outdated_heads = []
            orphan_heads = []
            digest = snap.current_dataset_digest()
            for head in heads:
                if head["dataset_digest"] == digest:
                    current_heads += 1
                    continue
                # An older head is expected after a correction/upgrade: the next
                # query recomputes and re-publishes.  It is only a problem if
                # the pointed-at cache row vanished (integritity) or if the head
                # refers to a digest that is not an older pin of this store.
                cached = snap.conn.execute(
                    "SELECT 1 FROM result_cache WHERE query_key=? AND dataset_digest=?",
                    (head["query_key"], head["dataset_digest"]),
                ).fetchone()
                if cached is None:
                    orphan_heads.append(
                        {"query_key": head["query_key"], "head": head["dataset_digest"]}
                    )
                else:
                    outdated_heads.append(
                        {
                            "query_key": head["query_key"],
                            "head": head["dataset_digest"],
                            "current": digest,
                        }
                    )
            return {
                "ok": not problems and not orphan_heads,
                "datasets": checked,
                "projection": projection,
                "dataset_digest": digest,
                "problems": problems,
                "orphan_heads": orphan_heads,
                "outdated_heads": outdated_heads,
                "current_heads": current_heads,
            }


# ---------------------------------------------------------------------------
# Document application (must run inside a write transaction)
# ---------------------------------------------------------------------------


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else 0


def _apply_document(conn: sqlite3.Connection, norm: CanonicalOrder, projection: int) -> dict:
    order_key = norm.order_key
    fingerprint = fingerprint_document(norm.doc)
    latest_row = conn.execute(
        "SELECT MAX(version) AS v FROM order_versions WHERE order_key=?", (order_key,)
    ).fetchone()
    latest = int(latest_row["v"]) if latest_row and latest_row["v"] is not None else 0
    now = utcnow()

    if latest:
        prev = conn.execute(
            "SELECT * FROM order_versions WHERE order_key=? AND version=?", (order_key, latest)
        ).fetchone()
        if prev["fingerprint"] == fingerprint:
            if prev["schema_version"] != norm.schema_version or int(prev["projection"]) != projection:
                # Same content, newer structural interpretation: re-project in
                # place without inventing a new business version.
                conn.execute(
                    "UPDATE order_versions SET schema_version=?, projection=? "
                    "WHERE order_key=? AND version=?",
                    (norm.schema_version, projection, order_key, latest),
                )
                _reproject(conn, norm, latest, projection, now)
                _write_plan(conn, norm, latest, projection, "structural upgrade", now)
                return {"version": latest, "fingerprint": fingerprint, "written": False, "upgraded": True}
            return {"version": latest, "fingerprint": fingerprint, "written": False, "upgraded": False}

    version = latest + 1
    conn.execute(
        "INSERT INTO order_versions(order_key, version, schema_version, projection, fingerprint, order_id, "
        "tenant, channel, status, placed_at, placed_date, currency, order_amount_cents, document, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_key,
            version,
            norm.schema_version,
            projection,
            fingerprint,
            norm.order_id,
            norm.tenant,
            norm.channel,
            norm.status,
            norm.placed_at.isoformat(),
            norm.placed_at.date().isoformat(),
            norm.currency,
            norm.order_amount,
            canonical_json(norm.doc),
            now,
        ),
    )
    _reproject(conn, norm, version, projection, now)
    reason = "initial load" if latest == 0 else "incremental correction"
    _write_plan(conn, norm, version, projection, reason, now)
    return {"version": version, "fingerprint": fingerprint, "written": True, "upgraded": False}


def _reproject(
    conn: sqlite3.Connection, norm: CanonicalOrder, version: int, projection: int, now: str
) -> None:
    conn.execute("DELETE FROM bridge_rows WHERE order_key=? AND version=?", (norm.order_key, version))
    conn.execute("DELETE FROM bridge_edges WHERE order_key=? AND version=?", (norm.order_key, version))

    def insert(grain: str, ord_: int, source_id: str, payload: dict, array_size: int) -> None:
        payload_json = canonical_json(payload)
        conn.execute(
            "INSERT INTO bridge_rows(order_key, version, grain, element_ord, source_id, array_size, "
            "payload, fingerprint, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                norm.order_key,
                version,
                grain,
                ord_,
                source_id,
                array_size,
                payload_json,
                _row_fingerprint(grain, source_id, payload),
                now,
            ),
        )

    insert(
        "order",
        -1,
        norm.order_id,
        {
            "order_id": norm.order_id,
            "tenant": norm.tenant,
            "channel": norm.channel,
            "status": norm.status,
            "placed_at": norm.placed_at.isoformat(),
            "placed_date": norm.placed_at.date().isoformat(),
            "currency": norm.currency,
            "order_amount_cents": norm.order_amount,
            "item_count": len(norm.items),
            "discount_count": len(norm.discounts),
            "fulfillment_count": len(norm.fulfillments),
            "source_schema_version": norm.schema_version,
            "position": "$",
        },
        len(norm.items),
    )
    for e in norm.items:
        insert("item", e.ord, e.business_id, e.payload, len(norm.items))
    for e in norm.discounts:
        insert("discount", e.ord, e.business_id, e.payload, len(norm.discounts))
    for e in norm.fulfillments:
        insert("fulfillment", e.ord, e.business_id, e.payload, len(norm.fulfillments))

    # Positional association bridge.  Sentinel -1 records an empty array
    # rather than dropping the order from the association entirely.
    item_ords = [e.ord for e in norm.items] or [-1]
    discount_ords = [e.ord for e in norm.discounts] or [-1]
    fulfillment_ords = [e.ord for e in norm.fulfillments] or [-1]
    for io in item_ords:
        for do in discount_ords:
            for fo in fulfillment_ords:
                conn.execute(
                    "INSERT OR IGNORE INTO bridge_edges"
                    "(order_key, version, item_ord, discount_ord, fulfillment_ord) VALUES(?,?,?,?,?)",
                    (norm.order_key, version, io, do, fo),
                )


def _canonical_plan(norm: CanonicalOrder, version: int, projection: int) -> Plan:
    return build_plan(
        order_key=norm.order_key,
        order_version=version,
        projection=projection,
        grain="order",
        filter_={"lifecycle": "latest"},
        shape={
            "items": len(norm.items),
            "discounts": len(norm.discounts),
            "fulfillments": len(norm.fulfillments),
        },
    )


def _write_plan(
    conn: sqlite3.Connection,
    norm: CanonicalOrder,
    version: int,
    projection: int,
    reason: str,
    now: str,
) -> None:
    plan = _canonical_plan(norm, version, projection)
    plan_json = canonical_json(plan.as_dict())
    plan_hash = plan_digest(plan)
    conn.execute(
        "INSERT INTO order_plan(order_key, order_version, projection, shape_json, plan_hash, plan_json, "
        "updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(order_key) DO UPDATE SET "
        "order_version=excluded.order_version, projection=excluded.projection, "
        "shape_json=excluded.shape_json, plan_hash=excluded.plan_hash, "
        "plan_json=excluded.plan_json, updated_at=excluded.updated_at",
        (norm.order_key, version, projection, canonical_json(plan.shape), plan_hash, plan_json, now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO plan_history"
        "(order_key, order_version, projection, plan_hash, plan_json, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (norm.order_key, version, projection, plan_hash, plan_json, reason, now),
    )


def _row_fingerprint(grain: str, source_id: str, payload: dict) -> str:
    body = canonical_json({"grain": grain, "source_id": source_id, "payload": payload})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
