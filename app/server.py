"""Minimal stdlib HTTP server for the analysis console.

No third-party dependencies.  The API is deliberately small:

    GET  /                      console page
    GET  /api/bootstrap         catalogue, orders, plan hashes, load runs
    POST /api/query             validate + execute + publish one query
    GET  /api/orders/<key>      one order: document, bridge rows, plan history
    GET  /api/ops               store / run / cache state
    GET  /api/audit             plan/data/version consistency audit
    POST /api/query             validate + execute + publish one query
    POST /api/load              load a fixture (base or corrections), optional interrupt/resume
    POST /api/upgrade           structural upgrade (bump projection)
    POST /api/resume            interrupt then resume a load, returning both runs
    POST /api/audit             run the consistency audit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.executor import QueryError  # noqa: E402
from app.service import Service  # noqa: E402
from app.store import StaleResult, VersionConflict  # noqa: E402

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
SERVICE: Service | None = None
SERVICE_LOCK = threading.RLock()


def service() -> Service:
    global SERVICE
    with SERVICE_LOCK:
        if SERVICE is None:
            SERVICE = Service.open()
            SERVICE.seed_if_empty()
        return SERVICE

def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrdersGrainConsole/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_file("index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                name = os.path.basename(unquote(path[len("/static/"):]))
                ctype = {
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }.get(os.path.splitext(name)[1], "application/octet-stream")
                self._serve_file(name, ctype)
            elif path == "/api/bootstrap":
                _json(self, 200, service().bootstrap())
            elif path == "/api/ops":
                _json(self, 200, service().ops_state())
            elif path == "/api/audit":
                _json(self, 200, {"ok": True, "audit": service().audit()})
            elif path.startswith("/api/orders/"):
                key = unquote(path[len("/api/orders/"):])
                _json(self, 200, service().order_detail(key))
            elif path == "/api/query":
                # Convenience for curl/links: GET with query params.
                qs = parse_qs(parsed.query)
                grain = (qs.get("grain") or ["order"])[0]
                metrics = (qs.get("metrics") or [""])[0]
                group_by = (qs.get("group_by") or [""])[0]
                result = service().query(
                    grain,
                    [m for m in metrics.split(",") if m],
                    [g for g in group_by.split(",") if g],
                    {},
                    use_cache=True,
                )
                _json(self, 200, result)
            else:
                _json(self, 404, {"ok": False, "error": f"not found: {path}"})
        except QueryError as exc:
            _json(self, 422, {"ok": False, "error": str(exc), "details": exc.details})
        except KeyError as exc:
            _json(self, 404, {"ok": False, "error": f"unknown order {exc}"})
        except Exception as exc:  # pragma: no cover - last-resort reporting
            traceback.print_exc()
            _json(self, 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _serve_file(self, name: str, ctype: str) -> None:
        safe = os.path.basename(name)
        full = os.path.join(WEB_ROOT, safe)
        if not os.path.isfile(full):
            _json(self, 404, {"ok": False, "error": f"missing asset {safe}"})
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            _json(self, 400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return
        try:
            if parsed.path == "/api/query":
                result = service().query(
                    str(payload.get("grain") or "order"),
                    list(payload.get("metrics") or []),
                    list(payload.get("group_by") or []),
                    dict(payload.get("filters") or {}),
                    use_cache=bool(payload.get("use_cache", True)),
                    publish=bool(payload.get("publish", True)),
                    detail=bool(payload.get("detail", False)),
                )
                _json(self, 200, result)
            elif parsed.path == "/api/load":
                fixture = str(payload.get("fixture") or "base")
                resume_of = payload.get("resume_of")
                interrupt_after = payload.get("interrupt_after")
                svc = service()
                path = svc.path_for(fixture) if hasattr(svc, "path_for") else None
                if path is None:
                    from app.service import CORRECTIONS, FIXTURE

                    path = CORRECTIONS if fixture == "corrections" else FIXTURE
                result = svc.load(
                    path,
                    interrupt_after=int(interrupt_after) if interrupt_after is not None else None,
                    resume_of=str(resume_of) if resume_of else None,
                )
                _json(self, 200, {"ok": True, "load": result, "ops": svc.ops_state()})
            elif parsed.path == "/api/upgrade":
                svc = service()
                upgrade = svc.upgrade()
                _json(self, 200, {"ok": True, "upgrade": upgrade, "ops": svc.ops_state()})
            elif parsed.path == "/api/resume":
                svc = service()
                after = int(payload.get("interrupt_after", 4))
                demo = svc.resume_demo(interrupt_after=after)
                _json(self, 200, {"ok": True, "resume": demo, "ops": svc.ops_state()})
            elif parsed.path == "/api/audit":
                _json(self, 200, {"ok": True, "audit": service().audit()})
            else:
                _json(self, 404, {"ok": False, "error": f"not found: {parsed.path}"})
        except QueryError as exc:
            _json(self, 422, {"ok": False, "error": str(exc), "details": exc.details})
        except (StaleResult, VersionConflict) as exc:
            _json(self, 409, {"ok": False, "error": str(exc), "code": type(exc).__name__})
        except KeyboardInterrupt:
            _json(self, 409, {"ok": False, "error": "load interrupted", "code": "interrupted"})
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            _json(self, 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)
    if args.db:
        os.environ["ORDERS_DB"] = args.db
    service()  # seed eagerly so the first page load is fast
    httpd = build_server(args.host, args.port)
    print(f"订单粒度分析台: http://{args.host}:{args.port}  (db={os.environ.get('ORDERS_DB', 'data/orders.sqlite3')})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
