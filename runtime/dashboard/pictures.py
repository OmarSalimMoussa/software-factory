"""Pictures and QA evidence: the bytes the warden keeps, served as images under a policy that lets nothing in them
run. The page draws them with <img> only."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from runtime.dashboard.client import Warden

POLICY = "default-src 'none'; style-src 'unsafe-inline'"
TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"})
PICTURE = re.compile(r"^/api/picture/(task|card)/([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
EVIDENCE = re.compile(
    r"^/api/evidence/(t-\d{4,})/((?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}/){0,6}[A-Za-z0-9][\w.-]{0,127})$"
)


@dataclass(frozen=True, slots=True)
class Image:
    status: int
    media: str
    body: bytes


def _missing(reason: str) -> Image:
    return Image(404, "text/plain; charset=utf-8", reason.encode())


def fetch(warden: Warden, path: str) -> Image | None:
    """The image at ``path``, a 404 saying why there is none, or None when ``path`` names no picture."""
    if found := PICTURE.match(path):
        answer = warden.call("picture", {"kind": found.group(1), "id": found.group(2)})
    elif found := EVIDENCE.match(path):
        answer = warden.call("evidence", {"task": found.group(1), "path": found.group(2)})
    else:
        return None
    if not answer.get("ok"):
        return _missing(str(answer.get("reason", "There is no such picture.")))
    media = answer.get("type")
    if media not in TYPES:
        return _missing("The warden sent something that is not an image.")
    try:
        body = base64.b64decode(str(answer.get("bytes_b64", "")), validate=True)
    # telemetry: surfaced — the page draws what `_missing` says: the picture could not be read
    except binascii.Error:
        return _missing("The picture could not be read.")
    return Image(200, str(media), body)
