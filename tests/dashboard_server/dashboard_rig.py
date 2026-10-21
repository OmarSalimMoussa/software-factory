"""The dashboard's test rig: a seeded state, a fake warden on it, and the dashboard's app asking it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fake_warden import FakeWarden, FixedClock
from settings_text import set_line

from runtime.dashboard.server import App, Reply
from runtime.shared import settings
from runtime.shared.paths import Places
from runtime.warden import public

HOST = {"host": "127.0.0.1:8300"}
OWNER = {**HOST, "cookie": "owner=1"}


@dataclass
class Rig:
    places: Places
    clock: FixedClock
    warden: FakeWarden
    app: App

    def get(self, url: str, headers: dict[str, str] | None = None) -> Reply:
        return self.app.get(url, OWNER if headers is None else headers)

    def parts(self, url: str) -> dict[str, Any]:
        reply = self.get(url)
        assert reply.status == 200, reply.body
        parts: dict[str, Any] = json.loads(reply.body)["parts"]
        return parts

    def post(self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Reply:
        sent = {**OWNER, "origin": "http://127.0.0.1:8300", "content-type": "application/json", "x-software": "1"}
        return self.app.post(url, sent if headers is None else headers, json.dumps(body).encode())

    def dashboard(self, **values: int) -> None:
        """Set these dashboard settings, as the warden reads them and publishes them for the dashboard."""
        text = self.places.defaults.read_text()
        for name, value in values.items():
            text = set_line(text, f"dashboard.{name}", value)
        self.places.defaults.write_text(text)
        public.write_settings(self.places, settings.load(self.places.defaults, self.places.overrides))
