"""Serves the dashboard UI and its fixtures for headless tests, with switches for every broken state.

Run: python3 tests/dashboard_ui/fixture_server.py [--port 0]. It prints "listening on <url>" once ready.
Test-only routes: POST /__switch (JSON merged into the switches; "scenario", "bump", "lock", "saved" and "reset"
act at once), GET /__calls (every API call so far), POST /__reset.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fixture_factory import Factory
from fixture_state import JSON, ok, refusal

ROOT = Path(__file__).resolve().parents[2]
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
PICTURE_CSP = "default-src 'none'; style-src 'unsafe-inline'"
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
LOCK = threading.Lock()


def strip(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: strip(x) for k, x in v.items() if not k.startswith("_")}
    if isinstance(v, list):
        return [strip(x) for x in v]
    return v


class Handler(BaseHTTPRequestHandler):
    server_version = "fixture"
    factory: Factory
    ui: Path
    pictures: Path

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self.send(status, json.dumps(strip(data)).encode(), "application/json", headers)

    def envelope(self, parts: JSON) -> JSON:
        f, now = self.factory, datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        out: JSON = {}
        for name, data in parts.items():
            if name in f.switches["fail_part"]:
                out[name] = {"ok": False, "error": f"The {name} could not be read: its file is missing."}
            elif isinstance(data, dict) and "$error" in data:
                out[name] = {"ok": False, "error": data["$error"]}
            else:
                out[name] = {"ok": True, "as_of": now, "data": data}
        return {"api": f.switches["api"], "as_of": now, "parts": out}

    def owner(self) -> bool:
        return "owner=1" in (self.headers.get("Cookie") or "")

    def gate(self, path: str) -> bool:
        """Answers for the factory being away, slow or failing; True when it already answered."""
        sw = self.factory.switches
        if sw["slow_ms"] and path.startswith(sw["slow_path"]):
            time.sleep(sw["slow_ms"] / 1000)
        if sw["down"]:
            self.json(503, {"api": sw["api"], "warden": {"ok": False, "since": "14:02"}, "parts": {}})
            return True
        if not self.owner() and path != "/api/device":
            self.json(401, refusal("device", "This device is not signed in."))
            return True
        return False

    # ---------- GET ----------
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path, q = url.path, dict(parse_qsl(url.query, keep_blank_values=True))
        if path == "/__calls":
            with LOCK:
                self.json(200, self.factory.calls)
            return
        if not path.startswith("/api/"):
            self.static(path)
            return
        with LOCK:
            self.factory.calls.append({"method": "GET", "path": path, "query": q})
        if self.gate(path):
            return
        if (fail := self.factory.switches["fail_path"]) and path.startswith(fail):
            self.json(500, {"error": "The dashboard hit an error building this page."})
            return
        if m := re.fullmatch(r"/api/(picture/(\w+)/([\w.-]+)|evidence/([\w.-]+)/([\w.-]+))", path):
            name = f"{m[2]}-{m[3]}.svg" if m[2] else f"evidence-{m[4]}-{m[5]}"
            file = self.pictures / name
            if file.is_file():
                self.send(
                    200,
                    file.read_bytes(),
                    "image/svg+xml",
                    {"Content-Security-Policy": PICTURE_CSP, "X-Content-Type-Options": "nosniff"},
                )
            else:
                self.json(404, {"error": "No such picture."})
            return
        with LOCK:
            parts = self.read(path, q)
        if parts is None:
            self.json(404, {"error": "No such endpoint."})
        else:
            self.json(200, self.envelope(parts))

    def read(self, path: str, q: dict[str, str]) -> JSON | None:
        f = self.factory
        page = int(q.get("page", "1"))
        simple: dict[str, Callable[[], JSON]] = {
            "/api/shell": f.shell_view,
            "/api/home": f.home_view,
            "/api/settings": lambda: {"settings": f.settings},
            "/api/guard": lambda: f.guard,
            "/api/push": lambda: {"push": f.push},
        }
        if path in simple:
            return simple[path]()
        if path == "/api/work":
            return f.work(q.get("tab", "tasks"), q.get("inbox", "all"), q.get("outcome", "all"), page)
        if path == "/api/agents":
            return f.agents_view(int(q.get("history_page", "1")))
        if path == "/api/spend":
            return f.spend(q.get("range", "14"), q.get("slice", "family"), page)
        if path == "/api/library":
            return f.library_view(q)
        if path == "/api/reflect":
            return f.reflect(q.get("kind", "all"), page)
        if path == "/api/search":
            return f.search_view(q.get("q", ""))
        if m := re.fullmatch(r"/api/agent/([\w.-]+)/stop-preview", path):
            return f.preview(m[1])
        routes = {"task": f.task, "agent": f.agent, "history": f.history, "doc": f.doc, "ops": f.op}
        if (m := re.fullmatch(r"/api/(task|agent|history|doc|ops)/([\w.-]+)", path)) and m[1] in routes:
            return routes[m[1]](m[2])
        return None

    def static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        file = (self.ui / rel).resolve()
        if not file.is_relative_to(self.ui) or not file.is_file() or file.suffix not in TYPES:
            self.send(404, b"not found", "text/plain")
            return
        headers = (
            {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"}
            if file.suffix == ".html"
            else {"X-Content-Type-Options": "nosniff"}
        )
        self.send(200, file.read_bytes(), TYPES[file.suffix], headers)

    # ---------- POST ----------
    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = None
        if path in ("/__switch", "/__reset"):
            self.switch(body if isinstance(body, dict) else {}, path == "/__reset")
            return
        with LOCK:
            self.factory.calls.append(
                {
                    "method": "POST",
                    "path": path,
                    "body": body,
                    "headers": {
                        "x-software": self.headers.get("X-Software"),
                        "content-type": self.headers.get("Content-Type"),
                    },
                }
            )
        if self.gate(path):
            return
        if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json" or self.headers.get(
            "X-Software"
        ) != "1":
            self.json(403, refusal("header", "A write needs JSON and the dashboard's header."))
            return
        if not isinstance(body, dict) or not isinstance(body.get("idempotency_key"), str):
            self.json(400, refusal("body", "A write needs a JSON body with an idempotency key."))
            return
        key = body["idempotency_key"]
        with LOCK:
            if key in self.factory.seen:
                status, answer = self.factory.seen[key]
            else:
                status, answer = self.write(path, body)
                self.factory.seen[key] = (status, answer)
        cookie = (
            {"Set-Cookie": "owner=1; HttpOnly; SameSite=Strict; Path=/"}
            if path == "/api/device" and answer.get("ok")
            else None
        )
        self.json(status, answer, cookie)

    def write(self, path: str, body: JSON) -> tuple[int, JSON]:
        f = self.factory
        for prefix, reason in f.switches["refuse"].items():
            if path.startswith(prefix):
                return 409, refusal("switch", reason)
        answer = self.route(path, body)
        if answer is None:
            return 404, refusal("unknown", "There is no such action.")
        if answer.get("ok"):
            return 200, answer
        return (403 if answer.get("code") else 409), answer

    def route(self, path: str, body: JSON) -> JSON | None:
        f = self.factory
        if path == "/api/device":
            return f.check_code(body) or ok("This device is signed in.", ["all"])
        if m := re.fullmatch(r"/api/proposals/([\w.-]+)/(approve|later|drop|move|run)", path):
            return f.proposal_act(m[1], m[2], body)
        if m := re.fullmatch(r"/api/approved/([\w.-]+)/move", path):
            return f.approved_move(m[1], body)
        if m := re.fullmatch(r"/api/tasks/([\w.-]+)/(boost|unboost)", path):
            return f.boost(m[1], m[2] == "boost")
        if m := re.fullmatch(r"/api/requests/([\w.-]+)/boost", path):
            return f.boost_request(m[1])
        if m := re.fullmatch(r"/api/parked/([\w.-]+)/unpark", path):
            return f.unpark(m[1])
        if m := re.fullmatch(r"/api/needs/([\w.-]+)/(go|feedback|extend|designer|park|answer)", path):
            return f.need_act(m[1], m[2], body)
        if m := re.fullmatch(r"/api/agents/([\w.-]+)/stop", path):
            return f.stop(m[1], body)
        if m := re.fullmatch(r"/api/rules/([\w.-]+)", path):
            return f.rule(m[1], body)
        fixed = {"/api/outage/stop": f.stop_outage_fix, "/api/orchestrator/start": f.start_orchestrator}
        if path in fixed:
            return fixed[path]()
        coded = {
            "/api/factory/pause": f.pause,
            "/api/factory/resume": f.resume,
            "/api/settings": f.save_settings,
            "/api/settings/repo": f.change_repo,
            "/api/push": f.push_prod,
            "/api/push/back": f.push_back,
        }
        return coded[path](body) if path in coded else None

    def switch(self, body: JSON, reset: bool) -> None:
        with LOCK:
            f = self.factory
            if reset or body.pop("reset", False):
                f.reset()
            if scenario := body.pop("scenario", None):
                f.scenario(scenario)
            if task := body.pop("bump", None):
                f.bump(task)
            if "lock" in body:
                f.shell["code"]["locked_until"] = body.pop("lock")
            for key, value in body.pop("saved", {}).items():
                row = next(r for g in f.settings["groups"] for r in g["rows"] if r.get("key") == key)
                row.update({"saved": value, "value": value})
            f.switches.update(body)
            self.json(200, f.switches)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ui", type=Path, default=ROOT / "runtime/dashboard/ui")
    ap.add_argument("--fixtures", type=Path, default=ROOT / "testdata/dashboard-fixtures")
    args = ap.parse_args()
    Handler.factory = Factory(args.fixtures)
    Handler.ui = args.ui.resolve()
    Handler.pictures = args.fixtures / "pictures"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
