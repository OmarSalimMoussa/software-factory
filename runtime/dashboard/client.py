"""The dashboard's one door to the warden: owner.sock, one JSON line in and one out per call."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import protocol
from runtime.shared.settings import Settings
from runtime.warden.verbs import VERBS


class WardenDown(Exception):
    """The warden gave ``verb`` no answer; ``why`` names the error's class, or what was wrong with what came back."""

    def __init__(self, verb: str, why: str) -> None:
        super().__init__(f"{verb}: {why}")
        self.verb, self.why = verb, why


def wait(settings: Settings, verb: str) -> int:
    """Seconds the dashboard waits for the warden's answer to ``verb``: longer for one that runs a product's script."""
    slow = verb in VERBS and VERBS[verb].slow
    return settings.get_int("dashboard.slow_write_seconds" if slow else "dashboard.warden_seconds")


def page_ms(settings: Settings, verb: str) -> int:
    """How long a page waits for the answer to ``verb``: the dashboard's wait, and a margin for the trip."""
    return (wait(settings, verb) + settings.get_int("dashboard.page_margin_seconds")) * 1000


class Warden:
    def __init__(self, path: Path, settings: Callable[[], Settings]) -> None:
        self.path, self.settings = path, settings

    def call(self, verb: str, args: dict[str, Any], data: bytes | None = None) -> dict[str, Any]:
        """The warden's answer, ``{"ok": true, ...}`` or a refusal; raises WardenDown when there is none."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(wait(self.settings(), verb))
                conn.connect(str(self.path))
                conn.sendall(protocol.request(verb, args, data))
                with conn.makefile("rb") as reader:
                    line = reader.readline(protocol.MAX_LINE + 1)
        except (OSError, TimeoutError) as exc:
            raise WardenDown(verb, type(exc).__name__) from exc
        try:
            answer = json.loads(line)
        except ValueError as exc:
            raise WardenDown(verb, "an answer that is not JSON") from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            raise WardenDown(verb, "an answer with no ok")
        return answer
