"""Store: element identity, versioned corrections, upgrades, resume."""

from __future__ import annotations

import json

from app.store import VersionConflict

from .conftest import base_docs, correction_docs, fresh_store, tmp_db


def test_bridge_rows_are_keyed_by_position():
    store = fresh_store()
    rows = store.rows_for("shop-a/ORD-1001", 1, "discount")
    assert [r["element_ord"] for r in rows] == [0, 1, 2]
    # Same code, different business id, both preserved.
    codes = [json.loads(r["payload"])["code"] for r in rows]
    assert codes.count("WELCOME") == 2
    assert {r["source_id"] for r in rows} == {"D1", "D2", "D3"}


def test_empty_array_is_recorded_not_dropped():
    store = fresh_store()
    rows = store.rows_for("shop-b/ORD-1004", 1)
    grains = [r["grain"] for r in rows]
    assert "item" not in grains
    assert grains.count("order") == 1
    # sentinel edge with item_ord = -1 keeps the order in the association bridge
    assert store.edge_count("shop-b/ORD-1004", 1) == 1


def test_edge_count_matches_cartesian_size_with_sentinels():
    store = fresh_store()
    assert store.edge_count("shop-a/ORD-1001", 1) == 3 * 3 * 2
    assert store.edge_count("shop-a/ORD-1005", 1) == 2 * 2 * 1
    assert store.edge_count("shop-c/ORD-1008", 1) == 1


def test_correction_appends_version_and_keeps_history():
    store = fresh_store()
    before = store.latest_states()
    state = next(s for s in before if s["order_key"] == "shop-a/ORD-1001")
    plan_before = store.plan_for("shop-a/ORD-1001")["plan_hash"]

    result = store.load("corrections", correction_docs())
    assert result.written == 1  # only ORD-1001 changed

    state2 = next(s for s in store.latest_states() if s["order_key"] == "shop-a/ORD-1001")
    assert state2["version"] == state["version"] + 1
    plan_after = store.plan_for("shop-a/ORD-1001")["plan_hash"]
    assert plan_before != plan_after
    history = store.plan_history("shop-a/ORD-1001")
    assert [h["order_version"] for h in history] == [1, 2]
    assert history[0]["reason"] == "initial load"
    assert history[1]["reason"] == "incremental correction"
    # old version rows are still present and unchanged
    old = store.rows_for("shop-a/ORD-1001", 1, "discount")
    assert len(old) == 3
    new = store.rows_for("shop-a/ORD-1001", 2, "discount")
    assert len(new) == 4


def test_unchanged_document_is_a_noop():
    store = fresh_store()
    before = store.stats()
    result = store.load("again", base_docs())
    after = store.stats()
    assert result.written == 0
    assert result.unchanged == len(base_docs())
    assert before["versions"] == after["versions"]
    assert before["bridge_rows"] == after["bridge_rows"]


def test_structural_upgrade_bumps_projection_and_replans():
    store = fresh_store()
    hashes = {s["order_key"]: s["plan_hash"] for s in store.latest_states()}
    new_hash = store.current_dataset_digest()

    upgrade = store.structural_upgrade()
    assert upgrade["projection"] == 1
    assert upgrade["orders"] == len(base_docs())
    states = store.latest_states()
    assert all(s["projection"] == 1 for s in states)
    assert {s["plan_hash"] for s in states} != set(hashes.values())
    assert store.current_dataset_digest() != new_hash
    # data unchanged by an upgrade
    assert store.stats()["bridge_rows"] == 37


def test_interrupted_load_resumes_exactly_once():
    reference = fresh_store()
    ref_states = {s["order_key"]: (s["version"], s["plan_hash"]) for s in reference.latest_states()}

    path = tmp_db()
    from app.store import Store

    store = Store(path)
    docs = base_docs()
    try:
        store.load("interrupt", docs, interrupt_after=4)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("load should have been interrupted")

    runs = [dict(r) for r in store.load_runs()]
    aborted = [r for r in runs if r["status"] == "aborted"]
    assert len(aborted) == 1
    assert aborted[0]["processed"] == 4
    # Only committed documents are visible.
    assert len(store.latest_states()) == 4

    resumed = store.load("interrupt", docs, resume_of=aborted[0]["run_id"])
    assert resumed.resumed_from == 4
    assert resumed.processed == len(docs)
    assert resumed.written == len(docs) - 4
    states = {s["order_key"]: (s["version"], s["plan_hash"]) for s in store.latest_states()}
    assert states == ref_states
    assert store.stats()["bridge_rows"] == reference.stats()["bridge_rows"]


def test_resume_rejects_changed_document_stream():
    path = tmp_db()
    from app.store import Store

    store = Store(path)
    docs = base_docs()
    try:
        store.load("interrupt", docs, interrupt_after=3)
    except KeyboardInterrupt:
        pass
    run_id = [r for r in map(dict, store.load_runs()) if r["status"] == "aborted"][-1]["run_id"]
    mutated = base_docs()
    mutated[1]["order_amount"] = 42
    try:
        store.load("interrupt", mutated, resume_of=run_id)
    except VersionConflict as exc:
        assert "resume mismatch" in str(exc)
    else:
        raise AssertionError("resume must reject a changed stream")


def test_audit_clean_after_normal_operations():
    store = fresh_store()
    assert store.audit()["ok"]
    store.load("corrections", correction_docs())
    assert store.audit()["ok"]
    store.structural_upgrade()
    audit = store.audit()
    assert audit["ok"], audit["problems"]
