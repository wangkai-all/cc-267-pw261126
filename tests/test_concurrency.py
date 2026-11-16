"""Concurrency: correction / upgrade / cache publication must not interleave
into an inconsistent plan-data-version state, and a stale result must never
overwrite a newer published one."""

from __future__ import annotations

import threading
import time

from app.executor import run_query
from app.store import StaleResult, Store

from .conftest import base_docs, correction_docs, tmp_db

QUERY = {"grain": "item", "metrics": ["item_discount_share", "item_net_amount", "pos_count"]}


def _recompute_after_correction(store: Store) -> dict:
    """Compute a result, then mutate the dataset before publishing."""
    from app.executor import _execute, _query_key, validate_query

    validation = validate_query(QUERY["grain"], QUERY["metrics"], [], {})
    with store.snapshot() as snap:
        result, digest, plan_set = _execute(
            snap,
            grain=QUERY["grain"],
            metrics=QUERY["metrics"],
            group_by=[],
            filters={},
            validation=validation,
            use_cache=False,
        )
    # A correction lands between computation and publication.
    store.load("corrections", correction_docs())
    return {
        "result": result,
        "digest": digest,
        "plan_set": plan_set,
        "query_key": _query_key(QUERY["grain"], QUERY["metrics"], [], {}, False),
    }


def test_stale_result_cannot_overwrite_newer_publication():
    store = Store(tmp_db())
    store.load("base", base_docs())
    stale = _recompute_after_correction(store)

    # Fresh computation on the corrected dataset publishes fine.
    fresh = run_query(
        store,
        grain=QUERY["grain"],
        metrics=QUERY["metrics"],
        use_cache=False,
    )
    fresh_digest = fresh["plan"]["dataset_digest"]
    assert fresh_digest != stale["digest"]

    try:
        store.publish_result(
            stale["query_key"],
            observed_dataset_digest=stale["digest"],
            grain=QUERY["grain"],
            plan_set=stale["plan_set"],
            payload=stale["result"],
        )
    except StaleResult as exc:
        assert "refusing to publish" in str(exc)
    else:
        raise AssertionError("stale result must be refused")

    hist = store.cache_history(stale["query_key"])
    assert any(h["dataset_digest"] == fresh_digest and h["is_head"] for h in hist)
    assert not any(h["dataset_digest"] == stale["digest"] for h in hist)


def test_run_query_reports_refused_publication_inline():
    """run_query computes and publishes inside one call; force a race by
    hooking the store's snapshot to mutate after the read."""
    store = Store(tmp_db())
    store.load("base", base_docs())
    original_snapshot = store.snapshot

    class RacingSnapshot:
        def __init__(self, snap):
            self._snap = snap

        def __getattr__(self, name):
            return getattr(self._snap, name)

    import contextlib
    from app import executor

    @contextlib.contextmanager
    def snapshot_and_race():
        with original_snapshot() as snap:
            yield RacingSnapshot(snap)
        # After the read transaction closes, a correction lands.
        if not getattr(snapshot_and_race, "done", False):
            snapshot_and_race.done = True
            store.load("corrections", correction_docs())

    store.snapshot = snapshot_and_race
    try:
        result = executor.run_query(
            store,
            grain=QUERY["grain"],
            metrics=QUERY["metrics"],
            use_cache=False,
        )
    finally:
        store.snapshot = original_snapshot
    assert result["published"]["status"] == "refused"
    assert any(w["kind"] == "stale_result" for w in result["warnings"])
    # The cancelled result is not in the cache.
    assert store.cache_history(
        result["cache"]["query_key"]
    ) == [] or all(
        h["dataset_digest"] != result["plan"]["dataset_digest"]
        for h in store.cache_history(result["cache"]["query_key"])
    )


def test_parallel_writers_leave_consistent_state():
    """Corrections + upgrades + queries on separate connections, interleaved."""
    store = Store(tmp_db())
    store.load("base", base_docs())
    docs = base_docs() + correction_docs()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(6):
                store.load("loop", docs)
                time.sleep(0.01)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def upgrader() -> None:
        try:
            for _ in range(3):
                store.structural_upgrade()
                time.sleep(0.02)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(8):
                run_query(store, grain="item", metrics=["item_net_amount"], use_cache=True)
                time.sleep(0.01)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=upgrader)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        if t.is_alive():  # pragma: no cover
            raise AssertionError("thread did not stop within 60s")
    if errors:
        raise errors[0]
    audit = store.audit()
    assert audit["ok"], audit["problems"]
    # every order is at the store projection
    states = store.latest_states()
    assert {s["projection"] for s in states} == {store.stats()["projection"]}


def test_upgrade_while_query_runs_does_not_mix():
    store = Store(tmp_db())
    store.load("base", base_docs())
    before = run_query(store, grain="item", metrics=["item_net_amount"], use_cache=False)
    digest_before = before["plan"]["dataset_digest"]
    upgrade = store.structural_upgrade()
    after = run_query(store, grain="item", metrics=["item_net_amount"], use_cache=False)
    assert after["plan"]["dataset_digest"] != digest_before
    assert after["plan"]["projection"] == upgrade["projection"]
    # Data values are unchanged by a structural upgrade...
    assert after["totals"] == before["totals"]
    # ...but the cached head now names the newer dataset.
    hist = store.cache_history(after["cache"]["query_key"])
    head = [h for h in hist if h["is_head"]]
    assert len(head) == 1
    assert head[0]["dataset_digest"] == after["plan"]["dataset_digest"]
    assert all(h["dataset_digest"] in {digest_before, after["plan"]["dataset_digest"]} for h in hist)
    assert any(h["dataset_digest"] == digest_before for h in hist)
