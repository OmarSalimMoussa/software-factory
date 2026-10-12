"""software-upgrading.service: the dashboard's stand-in while the dashboard's upgrade replaces the factory.

Run as ``/usr/bin/python3 -I <factory>/runtime/system/runners/upgrading.py <port> <seconds>``, as the dashboard's
user, on the dashboard's own address. Every page says the factory is upgrading, at which step, and reloads every
<seconds> until the dashboard answers there again; the API answers 503 with the same sentence, as the dashboard does
while the warden is away. It loads all its code at start, since the upgrade replaces the install's files under it.
"""

from __future__ import annotations

import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the factory's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.dashboard.server import BIND, UI
from runtime.shared import store, upgrades
from runtime.shared.paths import Places

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="refresh" content="{reload}">
<title>software factory</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<main class="page"><div class="empty">{note}</div></main>
</body>
</html>
"""


def note(places: Places) -> str:
    """What every answer says: the factory is upgrading, and at which step when root's record says."""
    try:
        progress = upgrades.read_progress(places)
    # telemetry: expected — no record yet, or one being replaced; the page says the factory is upgrading all the same
    except store.StoreError:
        progress = None
    step = progress["step"] if progress and progress["state"] == upgrades.RUNNING else ""
    now = f": {step[:1].lower()}{step[1:]}" if step else ""
    return f"The factory is upgrading{now}. This page comes back by itself when it is done."


class Handler(BaseHTTPRequestHandler):
    places: Places
    reload: int

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/app.css":
            self._stylesheet()
        elif path.startswith("/api/"):
            self._api()
        else:
            page = PAGE.format(reload=self.reload, note=html.escape(note(self.places)))
            self._send(503, page.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        self._api()

    def _api(self) -> None:
        said = note(self.places)
        body = {"ok": False, "warden": {"ok": False, "since": None, "note": said}, "error": said, "reason": said}
        self._send(503, json.dumps(body).encode(), "application/json")

    def _stylesheet(self) -> None:
        try:
            data = (UI / "app.css").read_bytes()
        # telemetry: expected — the upgrade swaps the install's files for a moment; the page's next reload has it
        except OSError:
            self._send(404, b"", "text/css; charset=utf-8")
            return
        self._send(200, data, "text/css; charset=utf-8")

    def _send(self, status: int, body: bytes, media: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: a page that reloads every few seconds is no news for the journal."""


def serve(places: Places, port: int, reload: int) -> ThreadingHTTPServer:
    bound = type("Bound", (Handler,), {"places": places, "reload": reload})
    return ThreadingHTTPServer((BIND, port), bound)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not all(arg.isdigit() for arg in argv):
        print("usage: upgrading.py <the dashboard's port> <seconds between reloads>", file=sys.stderr)
        return 2
    serve(Places.system(), int(argv[0]), int(argv[1])).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
