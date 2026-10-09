"""The small pictures every owner card carries (routing spec, R21): an SVG with nothing active in it, or a PNG.

An SVG is refused, never quietly trimmed, when it holds a script, a foreignObject, a style sheet, an event attribute
or any reference outside itself; one that passes is written back out from its parsed tree, so comments and
processing instructions are gone. A picture's name carries a hash of its bytes, so a new picture is a new name.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from runtime.queue.book import Queue
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TYPES = {"svg": "image/svg+xml", "png": "image/png"}
ALLOWED = frozenset(
    "svg g defs title desc path rect circle ellipse line polyline polygon text tspan linearGradient "
    "radialGradient stop clipPath mask pattern marker symbol use".split()
)
_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
_NAME = re.compile(r"^[a-z0-9-]+$")

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _bad(why: str) -> Refused:
    return Refused("picture", f"the picture {why}: send a plain SVG drawing or a PNG")


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _check_value(name: str, value: str) -> None:
    lowered = value.lower()
    if "javascript:" in lowered or "data:" in lowered or "@import" in lowered or "expression(" in lowered:
        raise _bad(f"has an active value in {name}")
    for _, target in _URL.findall(value):
        if not target.startswith("#"):
            raise _bad(f"refers outside itself in {name}")


def _clean_svg(data: bytes) -> bytes:
    head = data.lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise _bad("declares a doctype or an entity")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise _bad(f"is not well-formed XML ({exc})") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise _bad("is not an SVG document")
    for element in root.iter():
        ns, name = _local(element.tag)
        if ns != SVG_NS or name not in ALLOWED:
            raise _bad(f"holds a {name} element")
        for attr, value in element.attrib.items():
            attr_ns, attr_name = _local(attr)
            if attr_name.lower().startswith("on"):
                raise _bad(f"has an event attribute {attr_name}")
            if attr_ns not in ("", XLINK_NS):
                raise _bad(f"has a foreign attribute {attr_name}")
            if attr_name == "href" and not value.startswith("#"):
                raise _bad("links outside itself")
            _check_value(attr_name, value)
    cleaned = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    assert isinstance(cleaned, bytes)
    return cleaned


def clean(data: bytes | None, max_kib: int) -> tuple[bytes, str]:
    """The picture's bytes to keep and its kind, svg or png; refused when it is missing, too large or active."""
    if not data:
        raise Refused("picture", "a picture is required: --picture <file.svg|file.png>")
    if len(data) > max_kib * 1024:
        raise Refused("too-large", f"the picture is larger than queue.max_upload_kib ({max_kib} KiB)")
    if data.startswith(PNG_MAGIC):
        if data[12:16] != b"IHDR":
            raise _bad("is not a whole PNG")
        return data, "png"
    return _clean_svg(data), "svg"


def _name(prefix: str, data: bytes, kind: str) -> str:
    if not _NAME.match(prefix):
        raise ValueError(f"{prefix!r} cannot name a picture")
    return f"{prefix}-{hashlib.sha256(data).hexdigest()[:12]}.{kind}"


def task_picture(task_id: str, data: bytes, kind: str) -> str:
    """The queue path a task's picture takes; the caller writes it with the task."""
    return f"pictures/{_name(task_id, data, kind)}"


def save_card_picture(places: Places, card_id: str, data: bytes, kind: str) -> str:
    """A card's picture in the warden's own state; its file name."""
    name = _name(card_id, data, kind)
    places.pictures.mkdir(parents=True, exist_ok=True)
    (places.pictures / name).write_bytes(data)
    return name


def picture_bytes(places: Places, queue: Queue, kind: str, name: str) -> tuple[bytes, str]:
    """A picture for the dashboard: a task's from queue/pictures/, a card's from the warden's state."""
    stem, dot, ext = name.rpartition(".")
    if not dot or ext not in TYPES or not _NAME.match(stem):
        raise Refused("picture", f"{name!r} is not a picture name")
    if kind == "task":
        return queue.files.read(f"pictures/{name}"), TYPES[ext]
    if kind == "card":
        return (places.pictures / name).read_bytes(), TYPES[ext]
    raise Refused("picture", f"{kind!r} is not task or card")
