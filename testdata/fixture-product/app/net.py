"""The app's one door to the network: every call has a deadline and says when it fails."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class SyncError(Exception):
    """The sync service could not be reached or answered something unreadable."""


def fetch_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise SyncError(f"cannot sync: {type(exc).__name__}") from exc
