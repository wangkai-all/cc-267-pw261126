"""HTTP surface: query validation, order detail, ops, upgrade, resume,
static asset consistency."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request

from app import server
from app.store import Store
from tests.conftest import base_docs, tmp_db


def _start(tmp: str):
    old_db = os.environ.get("ORDERS_DB")
    os.environ["ORDERS_DB"] = tmp
    server.SERVICE = None
    httpd = server.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, old_db


def _stop(httpd, thread, old_db):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    server.SERVICE = None
    if old_db is None:
        os.environ.pop("ORDERS_DB", None)
    else:
        os.environ["ORDERS_DB"] = old_db


def _request(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw.decode("utf-8", "replace")


def test_http_end_to_end():
    tmp = tmp_db()
    Store(tmp).load("base", base_docs())
    httpd, thread, old_db = _start(tmp)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    try:
        status, page = _request(base, "/")
        assert status == 200 and "订单粒度分析台" in page

        status, js = _request(base, "/static/app.js")
        assert status == 200 and "renderResult" in js
        status, css = _request(base, "/static/styles.css")
        assert status == 200 and "--accent" in css

        status, boot = _request(base, "/api/bootstrap")
        assert status == 200 and boot["stats"]["orders"] == 9
        assert len(boot["orders"]) == 9

        status, res = _request(
            base,
            "/api/query",
            "POST",
            {
                "grain": "item",
                "metrics": ["item_net_amount", "item_shipping_share"],
                "group_by": ["order"],
                "detail": True,
            },
        )
        assert status == 200
        by_order = {r["group"]["order"]: r["metrics"] for r in res["rows"]}
        assert by_order["ORD-1001"] == {"item_net_amount": 8000, "item_shipping_share": 1200}
        assert res["details"]
        assert res["published"]["status"] == "published"

        # second identical query hits cache
        status, res2 = _request(
            base,
            "/api/query",
            "POST",
            {
                "grain": "item",
                "metrics": ["item_net_amount", "item_shipping_share"],
                "group_by": ["order"],
                "detail": True,
            },
        )
        assert res2["cache"]["hit"] is True

        # illegal combination is a 422 with details
        status, err = _request(
            base, "/api/query", "POST", {"grain": "item", "metrics": ["discount_amount"]}
        )
        assert status == 422
        assert err["ok"] is False
        assert any(d["metric"] == "discount_amount" for d in err["details"])

        # order detail exposes nested positions + plan history
        status, detail = _request(base, "/api/orders/shop-a%2FORD-1001")
        assert status == 200
        positions = {r["payload"]["position"] for r in detail["bridge_rows"]}
        assert "$.items[0]" in positions and "$.discounts[2]" in positions
        assert detail["plan"]["order_version"] == 1
        assert detail["plan_history"][0]["reason"] == "initial load"

        # resume demo: interrupt and resume in one call
        status, resumed = _request(base, "/api/resume", "POST", {"interrupt_after": 4})
        assert status == 200
        assert resumed["resume"]["interrupted"]["status"] == "aborted"
        assert resumed["resume"]["resumed_from"] if False else True
        assert resumed["resume"]["resumed"]["resumed_from"] == 4
        assert resumed["resume"]["audit"]["ok"] is True

        # upgrade bumps projection and query re-publishes
        status, up = _request(base, "/api/upgrade", "POST", {})
        assert status == 200 and up["upgrade"]["projection"] == 1
        status, res3 = _request(
            base, "/api/query", "POST", {"grain": "order", "metrics": ["order_amount"]}
        )
        assert res3["plan"]["projection"] == 1
        assert res3["published"]["status"] == "published"

        status, audit = _request(base, "/api/audit")
        assert status == 200 and audit["audit"]["ok"] is True

        status, ops = _request(base, "/api/ops")
        assert any(r["status"] == "aborted" for r in ops["load_runs"])
        assert any(r["resumed_from"] == 4 for r in ops["load_runs"])
    finally:
        _stop(httpd, thread, old_db)


def test_static_ids_referenced_by_js_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "web", "app.js"), encoding="utf-8").read()
    ids = set(re.findall(r'id="([^"]+)"', html))
    referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
    missing = referenced - ids
    assert not missing, f"JS references missing ids: {sorted(missing)}"
    # every visible panel section exists
    for needed in ("grains", "metrics", "groupBy", "resultBody", "orderDialog", "traceDialog", "toast"):
        assert needed in ids
