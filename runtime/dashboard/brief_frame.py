"""A designer's brief, served as a page of its own for the frame the brief page holds. Its scripts run, but sandboxed
to an origin of their own, so they reach neither the dashboard nor its sign-in, and the network only for the code
libraries a brief may load from the usual CDNs. Every brief gets one script more, ui/brief-frame.js: it draws the
brief's Mermaid diagrams, follows the dashboard's theme, tells the page what the owner selects, and highlights the
text each comment quotes. It speaks to the page by messages alone."""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from runtime.dashboard.client import Warden
from runtime.dashboard.pictures import Image

FRAME = re.compile(r"^/api/brief-page/(t-\d{4,})/([1-9][0-9]{0,5})$")
POLICY = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
    "img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
SCRIPT = (Path(__file__).resolve().parent / "ui" / "brief-frame.js").read_text(encoding="utf-8")


def _missing(reason: str) -> Image:
    return Image(404, "text/plain; charset=utf-8", reason.encode())


def with_script(page: str) -> str:
    """The brief with the frame's script last in its body."""
    tag = f"<script>{SCRIPT}</script>"
    end = page.lower().rfind("</body>")
    return page[:end] + tag + page[end:] if end >= 0 else page + tag


def fetch(warden: Warden, path: str) -> Image | None:
    """The brief page at ``path``, a 404 saying why there is none, or None when ``path`` names no brief page."""
    found = FRAME.match(path)
    if found is None:
        return None
    answer = warden.call("brief.page", {"task": found.group(1), "version": int(found.group(2))})
    if not answer.get("ok"):
        return _missing(str(answer.get("reason", "There is no such brief.")))
    try:
        page = base64.b64decode(str(answer.get("bytes_b64", "")), validate=True).decode("utf-8")
    # telemetry: surfaced — the frame draws what `_missing` says: the brief could not be read
    except (binascii.Error, UnicodeDecodeError):
        return _missing("The brief could not be read.")
    return Image(200, "text/html; charset=utf-8", with_script(page).encode())
