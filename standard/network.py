"""The standard commands' one door to the network: the deploy's look at the product's health and the server's
heartbeat send every outbound request through ``call``.

The caller sets the deadline, and may cap the body read (``limit``). An error status is an answer the caller reads;
redirects are followed. What does not answer raises OSError.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request


def call(url: str, timeout: float, *, data: bytes | None = None, limit: int | None = None) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as reply:
            return int(reply.status), reply.read(limit)
    # telemetry: expected — an error status is an answer: the caller reads its status and body
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read() if limit is None else exc.read(limit)
    except http.client.HTTPException as exc:
        raise OSError(f"{url} answered with something that is not HTTP ({type(exc).__name__})") from exc
