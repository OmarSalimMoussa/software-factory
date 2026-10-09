"""The dashboard's one door to the warden: owner.sock, one JSON line in and one out per call."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import protocol


class WardenDown(Exception):
    """The warden did not answer: its socket is gone, it refused the connection, or it answered too late."""


class Warden:
    def __init__(self, path: Path, timeout: Callable[[], float]) -> None:
        self.path, self.timeout = path, timeout

    def call(self, verb: str, args: dict[str, Any]) -> dict[str, Any]:
        """The warden's answer, ``{"ok": true, ...}`` or a refusal; raises WardenDown when there is none."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout())
                conn.connect(str(self.path))
                conn.sendall(protocol.request(verb, args))
                with conn.makefile("rb") as reader:
                    line = reader.readline(protocol.MAX_LINE + 1)
        except (OSError, TimeoutError) as exc:
            raise WardenDown(type(exc).__name__) from exc
        try:
            answer = json.loads(line)
        except ValueError as exc:
            raise WardenDown("an answer that is not JSON") from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            raise WardenDown("an answer with no ok")
        return answer
