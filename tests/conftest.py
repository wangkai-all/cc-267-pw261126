"""Shared fixtures for tests: isolated DB per test, fixture documents."""

from __future__ import annotations

import json
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "data", "orders.json")
CORRECTIONS = os.path.join(ROOT, "data", "corrections.json")


# Paths handed out by tmp_db(); the runner removes them after each test so a
# full suite run does not leave hundreds of databases in /tmp.
CREATED: list[str] = []


def tmp_db() -> str:
    fd, path = tempfile.mkstemp(prefix="orders-", suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    CREATED.append(path)
    return path


def cleanup_tmp() -> None:
    for path in CREATED:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass
    CREATED.clear()


def base_docs() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def correction_docs() -> list[dict]:
    with open(CORRECTIONS, encoding="utf-8") as fh:
        return json.load(fh)


def doc(order_id: str) -> dict:
    return next(d for d in base_docs() if d["order_id"] == order_id)


def fresh_store(docs: list[dict] | None = None):
    from app.store import Store

    store = Store(tmp_db())
    store.load("test", docs if docs is not None else base_docs())
    return store
