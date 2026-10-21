"""The owner's dashboard: its own process, serving the page from ui/ and the API from the warden's owner socket.

It serves the page even while the warden is down, and every page's last good parts, marked old. Each request runs on
a fixed pool of ``dashboard.max_threads`` threads. It reads only public/settings.json of the warden's state; all else
it asks the warden for. Run as ``python3 -I -m runtime.dashboard.server``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from runtime.dashboard import auth, brief_frame, cache, mcp, pictures
from runtime.dashboard.client import Warden, WardenDown, page_ms
from runtime.dashboard.views import ops, routes, writes
from runtime.dashboard.views.kit import PartError, Snap, clock, iso
from runtime.dashboard.views.tasks import proposals
from runtime.shared import error_reports, store
from runtime.shared import settings as settings_module
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

log = logging.getLogger("factory.dashboard")

UI = Path(__file__).resolve().parent / "ui"
BIND = "127.0.0.1"  # the network reaches the dashboard only through the box's edge
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}
_STATIC = re.compile(r"^/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+$")


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _json(status: int, data: Any, **headers: str) -> Reply:
    return Reply(status, json.dumps(data).encode(), {"Content-Type": "application/json", **headers})


def sentence(text: str) -> str:
    text = text.strip()
    return (text[:1].upper() + text[1:]).rstrip(".") + "." if text else "The factory refused that."


class PublicSettings:
    """The effective settings the warden publishes, read again whenever it replaces them."""

    def __init__(self, places: Places) -> None:
        self.path = places.state / "public" / "settings.json"
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._settings: Settings | None = None

    def __call__(self) -> Settings:
        info = self.path.stat()
        with self._lock:
            if self._settings is None or self._stamp != (info.st_ino, info.st_mtime_ns):
                tree = store.read_json(self.path)
                self._settings = settings_module.build(tree, {})
                self._stamp = (info.st_ino, info.st_mtime_ns)
            return self._settings


class App:
    def __init__(
        self, settings: Callable[[], Settings], warden: Warden, now: Callable[[], float], ui: Path = UI
    ) -> None:
        self.settings, self.warden, self.now, self.ui = settings, warden, now, ui
        self.parts = cache.Parts(now)
        self.devices = auth.Devices(warden, now)

    # ---- reads ---------------------------------------------------------------------------------------------------

    def get(self, target: str, headers: Mapping[str, str]) -> Reply:
        split = urlsplit(target)
        path, query = split.path, dict(parse_qsl(split.query))
        try:
            auth.check_host(headers, self.settings().get_list("dashboard.hosts"), write=False)
            if path == mcp.PATH:  # Claude Code asks here for an event stream, which this MCP never sends
                return Reply(405, b"", {"Allow": "POST"})
            return self._api(target, path, query, headers) if path.startswith("/api/") else self._static(path)
        # telemetry: surfaced — the browser is answered with `_json` saying why access was refused
        except auth.Denied as exc:
            return _json(exc.status, {"ok": False, "refused": "access", "reason": str(exc)})
        except WardenDown as lost:
            return self._away(target, lost)

    def _api(self, target: str, path: str, query: dict[str, str], headers: Mapping[str, str]) -> Reply:
        if (refused := self._device(auth.token_of(headers))) is not None:
            return refused
        image = pictures.fetch(self.warden, path)
        if image is not None:
            return Reply(
                image.status, image.body, {"Content-Type": image.media, "Content-Security-Policy": pictures.POLICY}
            )
        brief = brief_frame.fetch(self.warden, path)
        if brief is not None:
            return Reply(
                brief.status, brief.body, {"Content-Type": brief.media, "Content-Security-Policy": brief_frame.POLICY}
            )
        if found := routes.OP.match(path):
            return self._op(found.group(1))
        page = routes.find(path, query)
        if page is None:
            return _json(404, {"ok": False, "error": "There is no such page."})
        return self._page(target, *page)

    def _device(self, token: str | None) -> Reply | None:
        """None when the device is admitted, else the answer saying why not. A device the warden never confirmed is
        not let in while the warden is away, so what the dashboard last knew never reaches a stranger."""
        try:
            if self.devices.admitted(token):
                return None
        except WardenDown as lost:
            return self._unreachable("Can't reach the warden, so nothing changed.", lost)
        return _json(401, {"ok": False, "error": "This device is not signed in."})

    def _unreachable(self, reason: str, lost: WardenDown) -> Reply:
        self._warden_lost(lost)
        warden = {"ok": False, "since": clock(self.parts.down_since or self.now())}
        return _json(503, {"ok": False, "warden": warden, "error": "Can't reach the warden.", "reason": reason})

    def _timed(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return {**envelope, "timeout_ms": page_ms(self.settings(), "snapshot")}

    def _away(self, key: str, lost: WardenDown) -> Reply:
        self._warden_lost(lost)
        envelope = self.parts.stale(key, None)
        if envelope is not None:
            return _json(200, self._timed(envelope))
        return self._unreachable("Can't reach the warden.", lost)

    def _warden_lost(self, lost: WardenDown) -> None:
        if self.parts.down_since is None:
            log.warning(
                "dashboard: the warden does not answer %s on its owner socket (WardenDown: %s)", lost.verb, lost.why
            )
        self.parts.down()

    def _snapshot(self, wants: dict[str, Any]) -> Snap:
        answer = self.warden.call("snapshot", wants)
        if not answer["ok"]:
            raise PartError(sentence(str(answer.get("reason", ""))))
        return Snap(answer["now"], answer["state"], answer["errors"])

    def _page(self, key: str, page: routes.Page, query: dict[str, str]) -> Reply:
        try:
            snap = self._snapshot(page.wants(query))
            builders = page.parts(snap, query)
        # telemetry: surfaced — the browser is answered with `_json` saying what the page could not read
        except PartError as exc:
            return _json(400, {"ok": False, "error": str(exc)})
        built: dict[str, Any] = {}
        for name, build in builders.items():
            try:
                built[name] = {"ok": True, "as_of": iso(snap.now), "data": build()}
            # telemetry: surfaced — the part is `built` with its sentence, drawn in its place; the rest still draws
            except PartError as exc:
                built[name] = {"ok": False, "error": str(exc)}
            except Exception as exc:
                log.exception("dashboard: the %s part of %s failed (%s)", name, urlsplit(key).path, type(exc).__name__)
                built[name] = {
                    "ok": False,
                    "error": f"It could not be built ({type(exc).__name__}); the dashboard's log has the details.",
                }
        return _json(200, self._timed(self.parts.fresh(key, built)))

    def _op(self, op_id: str) -> Reply:
        answer = self.warden.call("op.get", {"id": op_id})
        if answer["ok"]:
            part = {"ok": True, "as_of": iso(self.now()), "data": ops.operation(answer)}
        else:
            part = {"ok": False, "error": sentence(str(answer.get("reason", "")))}
        return _json(200, self._timed(self.parts.fresh(f"/api/ops/{op_id}", {"op": part})))

    def _static(self, path: str) -> Reply:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if not _STATIC.match("/" + name) or ".." in name.split("/"):
            return _json(404, {"ok": False, "error": "There is no such page."})
        file = self.ui / name
        media = _TYPES.get(file.suffix)
        if media is None or not file.is_file():
            return _json(404, {"ok": False, "error": "There is no such page."})
        return Reply(200, file.read_bytes(), {"Content-Type": media, "Cache-Control": "no-cache"})

    # ---- writes --------------------------------------------------------------------------------------------------

    def post(self, target: str, headers: Mapping[str, str], raw: bytes) -> Reply:
        path = urlsplit(target).path
        try:
            return self._mcp(headers, raw) if path == mcp.PATH else self._change(path, headers, raw)
        # telemetry: surfaced — the caller is answered with `_json` saying why access was refused
        except auth.Denied as exc:
            return _json(exc.status, {"ok": False, "refused": "access", "reason": str(exc)})
        except WardenDown as lost:
            return self._unreachable("Can't reach the warden, so nothing changed.", lost)

    def _change(self, path: str, headers: Mapping[str, str], raw: bytes) -> Reply:
        host = auth.check_host(headers, self.settings().get_list("dashboard.hosts"), write=True)
        try:
            body = json.loads(raw) if raw else None
        except ValueError as exc:
            raise auth.Denied(400, "A change is one JSON object.") from exc
        key = auth.check_write(headers, body)
        assert isinstance(body, dict)  # check_write refused anything else
        if path == "/api/device":
            return self._admit(body, key, host)
        if (refused := self._device(auth.token_of(headers))) is not None:
            return refused
        found = writes.find(path)
        if found is None:
            return _json(404, {"ok": False, "error": "There is no such action."})
        return self._write(*found, body, key)

    def _write(self, write: writes.Write, ref: str, body: dict[str, Any], key: str) -> Reply:
        snap = None
        try:
            if write.wants:
                snap = self._snapshot({"want": ["settings", *write.wants]})
                if write.check is not None:
                    write.check(body, ref, snap)
            args = write.args(body, ref, snap)
            data = write.data(body) if write.data is not None else None
        # telemetry: surfaced — the browser is answered with `_json`: the owner saw an older state
        except writes.Stale as exc:
            return _json(409, {"ok": False, "refused": "stale", "reason": str(exc)})
        # telemetry: surfaced — the browser is answered with `_json` saying what is wrong with the input
        except PartError as exc:
            return _json(400, {"ok": False, "refused": "input", "reason": str(exc)})
        answer = self.warden.call(write.verb, {**args, "idempotency_key": key}, data)
        if not answer["ok"]:
            return self._refused(answer)
        if write.settle and snap is not None:
            proposals.settle(snap, ref, write.settle)
        reply: dict[str, Any] = {"ok": True, "refresh": write.refresh, "message": write.message(answer, snap)}
        for key in ("op", "open"):  # an operation the page follows; an address it opens in a new tab
            if isinstance(answer.get(key), str):
                reply[key] = answer[key]
        return _json(200, reply)

    def _mcp(self, headers: Mapping[str, str], raw: bytes) -> Reply:
        # Claude Code sends no Origin, so only a foreign one is refused: no browser sends a bearer token by itself.
        auth.check_host(headers, self.settings().get_list("dashboard.hosts"), write=False)
        if (refused := self._device(auth.bearer_of(headers))) is not None:
            return refused
        status, answer = mcp.answer(self.warden, self._warden_lost, raw)
        return Reply(status, b"") if answer is None else _json(status, answer)

    def _admit(self, body: dict[str, Any], key: str, host: str) -> Reply:
        answer = self.warden.call("device.admit", {"code": body.get("code"), "idempotency_key": key})
        if not answer["ok"]:
            return self._refused(answer)
        cookie = auth.owner_cookie(str(answer["token"]), host, self.settings())
        reply = {"ok": True, "refresh": ["all"], "message": "This device is signed in."}
        return _json(200, reply, **{"Set-Cookie": cookie})

    def _refused(self, answer: dict[str, Any]) -> Reply:
        rule, reason = str(answer.get("refused", "")), str(answer.get("reason", ""))
        out: dict[str, Any] = {"ok": False, "refused": rule, "reason": sentence(reason)}
        detail = answer.get("detail") or {}
        if rule == "code" and isinstance(detail.get("tries_left"), int):
            out["code"] = {"tries_left": detail["tries_left"]}
        elif rule == "code-locked":
            until = self.warden.call("snapshot", {"want": ["factory"]})["state"]["factory"]["locked_until"]
            if until is not None:
                out["code"] = {"locked_until": clock(until)}
        # A wrong or locked code is the owner's to retry (403); any other refusal is a conflict with the state (409).
        return _json(403 if "code" in out else 409, out)


# ---- HTTP ------------------------------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _PooledServer

    def _send(self, reply: Reply) -> None:
        self.send_response(reply.status)
        headers = {
            "Content-Security-Policy": auth.PAGE_POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            **reply.headers,
            "Content-Length": str(len(reply.body)),
        }
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.body)

    def _guarded(self, answer: Callable[[], Reply]) -> None:
        try:
            reply = answer()
        except Exception as exc:
            log.exception("dashboard: %s %s failed (%s)", self.command, urlsplit(self.path).path, type(exc).__name__)
            reply = _json(
                500, {"ok": False, "error": f"The dashboard failed ({type(exc).__name__}); its log has the details."}
            )
        self._send(reply)

    def do_GET(self) -> None:  # the name http.server calls
        self._guarded(lambda: self.server.app.get(self.path, {k.lower(): v for k, v in self.headers.items()}))

    def do_POST(self) -> None:
        def answer() -> Reply:
            size = int(self.headers.get("Content-Length") or 0)
            limit = self.server.app.settings().get_int("queue.max_upload_kib") * 1024
            if size > limit:
                return _json(413, {"ok": False, "refused": "access", "reason": "That change is too large."})
            return self.server.app.post(
                self.path, {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(size)
            )

        self._guarded(answer)

    def log_message(self, format: str, *args: Any) -> None:
        """Requests are not logged one by one; failures are, where they happen."""


class _PooledServer(HTTPServer):
    """An HTTP server whose requests run on a fixed pool of threads."""

    def __init__(self, address: tuple[str, int], app: App, threads: int) -> None:
        self.app = app
        self.pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="dashboard")
        super().__init__(address, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        self.pool.submit(self._work, request, client_address)

    def _work(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        # telemetry: expected — the browser left before its answer was sent (a reload, a closed tab)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("dashboard: a connection failed (%s)", type(exc).__name__)
        finally:
            self.shutdown_request(request)


def serve(app: App, port: int, threads: int, read_seconds: int) -> _PooledServer:
    """Start answering on 127.0.0.1:``port``; returns the server so a test can shut it down."""
    _Handler.timeout = read_seconds
    server = _PooledServer((BIND, port), app, threads)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    places = Places.system()
    settings = PublicSettings(places)
    current = settings()
    try:
        error_reports.install(places, timeout=current.get_int("watch.http_timeout_seconds"))
    except (error_reports.DsnError, OSError) as exc:
        log.critical("dashboard: it does not start without the factory's GlitchTip project: %s", exc)
        return 2
    warden = Warden(places.owner_socket, settings)
    app = App(settings, warden, time.time)
    port = current.get_int("dashboard.port")
    server = serve(app, port, current.get_int("dashboard.max_threads"), current.get_int("watch.socket_read_seconds"))
    log.info("dashboard: serving on %s:%d", BIND, port)
    threading.Event().wait()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
