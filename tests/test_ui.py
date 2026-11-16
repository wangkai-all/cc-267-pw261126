"""DOM-level smoke test of web/app.js against a real HTTP server.

The environment has no browser, so `tests/ui_smoke.mjs` runs the unmodified
`web/app.js` inside a tiny DOM shim and drives it like a user: pick a grain,
tick metrics, run, open the trace and order dialogs, upgrade.  This wrapper
only orchestrates server + node; it is skipped when node is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

from app import server
from app.store import Store

from .conftest import base_docs, tmp_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_ui_smoke_against_real_server():
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment dependent
        print("  skip ui smoke: node not installed")
        return

    tmp = tmp_db()
    Store(tmp).load("base", base_docs())
    old_db = os.environ.get("ORDERS_DB")
    os.environ["ORDERS_DB"] = tmp
    server.SERVICE = None
    httpd = server.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        env = dict(os.environ, APP_BASE=f"http://{host}:{port}")
        proc = subprocess.run(
            [node, os.path.join(ROOT, "tests", "ui_smoke.mjs")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        server.SERVICE = None
        if old_db is None:
            os.environ.pop("ORDERS_DB", None)
        else:
            os.environ["ORDERS_DB"] = old_db

    if proc.returncode != 0:  # pragma: no cover - failure detail
        raise AssertionError(
            "ui smoke failed:\nSTDOUT:\n%s\nSTDERR:\n%s" % (proc.stdout, proc.stderr)
        )
    assert "ui smoke ok" in proc.stdout
