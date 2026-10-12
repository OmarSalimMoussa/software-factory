"""The factory's own errors, sent in Sentry's envelope format to the factory's own GlitchTip: an instance apart from
the one the products' slots report to (runtime/system/errors/).

The warden and the dashboard call ``install`` once at start, and refuse to start without the project's address (the
``factory-dsn`` secret the install writes): every log record at ERROR or above goes there as one event. Sending
never raises into the code that logged: a failed send prints one line to stderr, and the sends after it stay quiet
until one works again. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import traceback
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from runtime.shared import network
from runtime.shared.paths import Places

ENVIRONMENT = "factory"
CLIENT = "software-factory/1"
CONTENT_TYPE = "application/x-sentry-envelope"
log = logging.getLogger("factory.errors")

Send = Callable[[urllib.request.Request, float], None]


class DsnError(ValueError):
    """The saved address is not a DSN the factory can send to; the message lists every problem."""


@dataclass(frozen=True, slots=True)
class Dsn:
    envelope_url: str
    public_key: str

    @property
    def auth(self) -> str:
        return f"Sentry sentry_version=7, sentry_client={CLIENT}, sentry_key={self.public_key}"


def dsn_path(places: Places) -> Path:
    """Written by the installer: warden-owned, and bound read-only into the dashboard's unit."""
    return places.state / "secrets" / "factory-dsn"


def parse_dsn(text: str) -> Dsn:
    """``<scheme>://<public key>@<host>[:port][/prefix]/<project id>``, as GlitchTip prints it."""
    parts = urlsplit(text.strip())
    prefix, _, project = parts.path.rstrip("/").rpartition("/")
    problems = []
    if parts.scheme not in ("http", "https"):
        problems.append("the scheme is not http or https")
    if not parts.username:
        problems.append("it names no public key before the @")
    host = parts.netloc.rpartition("@")[2]
    if not host:
        problems.append("it names no host")
    if not project.isdigit():
        problems.append("it does not end in a project number")
    if parts.query or parts.fragment:
        problems.append("it carries a query or a fragment")
    if problems:
        raise DsnError("the factory's DSN is unusable: " + "; ".join(problems))
    return Dsn(f"{parts.scheme}://{host}{prefix}/api/{project}/envelope/", parts.username or "")


def _frame(frame: traceback.FrameSummary, root: Path) -> dict[str, Any]:
    path = Path(frame.filename)
    inside = path.is_relative_to(root)
    found: dict[str, Any] = {
        "filename": str(path.relative_to(root)) if inside else frame.filename,
        "abs_path": frame.filename,
        "function": frame.name,
        "lineno": frame.lineno,
        "in_app": inside,
    }
    if frame.line:
        found["context_line"] = frame.line
    return found


def _exceptions(exc: BaseException, root: Path) -> list[dict[str, Any]]:
    """The exception and what caused it, oldest first, as Sentry orders a chain."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return [
        {
            "type": type(item).__name__,
            "module": type(item).__module__,
            "value": str(item),
            "stacktrace": {"frames": [_frame(f, root) for f in traceback.extract_tb(item.__traceback__)]},
        }
        for item in reversed(chain)
    ]


def event(record: logging.LogRecord, *, release: str, root: Path, server: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "platform": "python",
        "level": "fatal" if record.levelno >= logging.CRITICAL else "error",
        "logger": record.name,
        "logentry": {"formatted": record.getMessage()},
        "release": release,
        "environment": ENVIRONMENT,
        "server_name": server,
    }
    if record.exc_info and record.exc_info[1] is not None:
        body["exception"] = {"values": _exceptions(record.exc_info[1], root)}
    return body


def envelope(body: dict[str, Any], sent_at: datetime) -> bytes:
    """Three lines: the envelope's header, the one item's header, then the event."""
    payload = json.dumps(body, separators=(",", ":"), default=str).encode()
    header = {"event_id": body["event_id"], "sent_at": sent_at.isoformat()}
    item = {"type": "event", "length": len(payload), "content_type": "application/json"}
    return b"\n".join([json.dumps(header).encode(), json.dumps(item).encode(), payload]) + b"\n"


def send_event(request: urllib.request.Request, timeout: float) -> None:
    network.call(request, timeout, follow=True).checked("GlitchTip")


class Reporter(logging.Handler):
    """Sends each record at ERROR or above as one event, in the thread that logged it."""

    def __init__(self, dsn: Dsn, *, release: str, root: Path, timeout: float, send: Send = send_event) -> None:
        super().__init__(logging.ERROR)
        # Not self.release: that is the handler lock's own method.
        self.dsn, self.commit, self.root, self.timeout, self._send = dsn, release, root, timeout, send
        self.server = socket.gethostname()
        self.failing = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            body = envelope(event(record, release=self.commit, root=self.root, server=self.server), datetime.now(UTC))
            request = urllib.request.Request(
                self.dsn.envelope_url,
                data=body,
                method="POST",
                headers={"Content-Type": CONTENT_TYPE, "X-Sentry-Auth": self.dsn.auth},
            )
            self._send(request, self.timeout)
        # Logging here would come straight back to this handler, so the one line goes to stderr instead.
        except Exception as exc:  # noqa: BLE001 -- reporting an error must never raise one
            if not self.failing:
                print(f"error-reports: an error could not be sent to GlitchTip ({type(exc).__name__}); "
                      "later ones stay quiet until a send works", file=sys.stderr, flush=True)  # fmt: skip
            self.failing = True
            return
        if self.failing:
            print("error-reports: sending to GlitchTip works again", file=sys.stderr, flush=True)
        self.failing = False


def install(places: Places, *, timeout: float, send: Send = send_event) -> Reporter:
    """Report every error the service logs. Raises DsnError when the install's DSN is missing or unusable."""
    path = dsn_path(places)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DsnError(f"{path} is missing: the address of the factory's own GlitchTip, which the install writes. "
                       "Install the factory again.") from exc  # fmt: skip
    dsn = parse_dsn(text)
    commit = places.install / "COMMIT"
    release = commit.read_text(encoding="utf-8").strip()
    if not release:
        raise ValueError(f"{commit} names no commit: reinstall the factory")
    reporter = Reporter(dsn, release=release, root=places.install, timeout=timeout, send=send)
    logging.getLogger().addHandler(reporter)
    return reporter
