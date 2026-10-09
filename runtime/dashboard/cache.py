"""The last good answer of every part, so the page still draws, marked old, while the warden is away.

Every page answer is an envelope: the API version, when it was built, whether the warden answered (and since when it
has not), and each part as ``{"ok": true, "as_of", "data"}`` or ``{"ok": false, "error"}``. A part that failed keeps
its error; a part served from memory carries ``"old": true`` and the time it was built.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from runtime.dashboard.views.kit import API, clock, iso

# A memory bound, not a behaviour: the pages a few devices keep open fit many times over.
_KEEP = 256
NEVER_LOADED = "Can't reach the warden, and this part was never loaded since the dashboard started."


class Parts:
    def __init__(self, now: Callable[[], float]) -> None:
        self.now = now
        self._lock = threading.Lock()
        self._kept: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.down_since: float | None = None

    def warden(self) -> dict[str, Any]:
        with self._lock:
            since = self.down_since
        return {"ok": True} if since is None else {"ok": False, "since": clock(since)}

    def fresh(self, key: str, parts: dict[str, Any]) -> dict[str, Any]:
        """The envelope of a page the warden just answered; its good parts are kept for when it is away."""
        with self._lock:
            self.down_since = None
            kept = self._kept.setdefault(key, {})
            kept.update({name: part for name, part in parts.items() if part["ok"]})
            self._kept.move_to_end(key)
            while len(self._kept) > _KEEP:
                self._kept.popitem(last=False)
        return {"api": API, "as_of": iso(self.now()), "warden": {"ok": True}, "parts": parts}

    def stale(self, key: str, names: list[str] | None) -> dict[str, Any] | None:
        """The page from memory, every part marked old; None when this page was never answered."""
        with self._lock:
            if self.down_since is None:
                self.down_since = self.now()
            kept = self._kept.get(key)
            since = self.down_since
        if kept is None:
            return None
        wanted = names if names is not None else list(kept)
        parts = {n: {**kept[n], "old": True} if n in kept else {"ok": False, "error": NEVER_LOADED} for n in wanted}
        return {"api": API, "as_of": iso(self.now()), "warden": {"ok": False, "since": clock(since)}, "parts": parts}

    def down(self) -> None:
        with self._lock:
            if self.down_since is None:
                self.down_since = self.now()
