"""Read-only telemetry for agents, `factory errors` and `factory traces`, and the GlitchTip reads the errors import
shares.

The warden holds every key and asks GlitchTip and Langfuse itself; an agent sees only the answer. Every workspace,
prod and each slot (slot 0 is staging), lives on this machine's shared services, reached with the keys in the secrets'
server.json that the server set-up wrote. A workspace's GlitchTip projects are named for it (prod-…, slot0-…,
slot3-…) and its Langfuse traces carry its name as their environment, so a read never strays into another workspace,
or into the factory's own project. Every answer is cut to telemetry.max_kib. A key never appears in an answer, an
error or a log line.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.shared import network, store
from runtime.shared.network import Answer
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.system import layout
from runtime.warden.context import Context

PROD, SLOT = "prod", "slot"
_ISSUE_ID = re.compile(r"^[0-9]{1,20}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LINK = re.compile(r'<[^>]*>((?:\s*;\s*[A-Za-z]+="[^"]*")*)')
_ATTR = re.compile(r'([A-Za-z]+)="([^"]*)"')


class TelemetryError(Exception):
    """GlitchTip or Langfuse could not be read; the message says which and why, never a key."""


class NotSetUp(TelemetryError):
    """The keys that reach a service are not in place yet; the message says which set-up has not run."""


@dataclass(frozen=True, slots=True)
class GlitchTip:
    url: str
    org: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Langfuse:
    url: str
    public_key: str
    secret_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Issue:
    id: str
    project: str  # the project's slug
    title: str | None
    culprit: str | None
    level: str | None
    first_seen: str | None
    last_seen: str | None
    count: str | None
    permalink: str | None


def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
    """Raises OSError when the service cannot be reached. A redirect is an answer, never followed: it would carry the
    key to wherever it points."""
    return network.call(urllib.request.Request(url, headers={"Accept": "application/json", **headers}), timeout)


def get_json(service: str, url: str, headers: dict[str, str], timeout: float) -> tuple[Any, dict[str, str]]:
    where = urllib.parse.urlsplit(url).path
    try:
        reply = http_get(url, headers, timeout)
    except (OSError, TimeoutError) as exc:
        raise TelemetryError(f"{service} did not answer {where} ({type(exc).__name__})") from exc
    if reply.status != 200:
        raise TelemetryError(f"{service} answered {where} with HTTP {reply.status}")
    try:
        return json.loads(reply.body), reply.headers
    except ValueError as exc:
        raise TelemetryError(f"{service} answered {where} with something that is not JSON") from exc


def server_json(places: Places) -> Path:
    return layout.secrets(places.state) / layout.SERVER_KEYS


def read_keys(path: Path, section: str, names: tuple[str, ...], missing: str) -> dict[str, str]:
    """The named values of one section of a JSON key file."""
    try:
        raw = store.read_json(path)
    except FileNotFoundError as exc:
        raise NotSetUp(missing) from exc
    except store.StoreError as exc:
        raise TelemetryError(f"{path} is not JSON") from exc
    found = raw.get(section) if isinstance(raw, dict) else None
    where = f"{path.name} {section}"
    if not isinstance(found, dict):
        raise TelemetryError(f"{where} is not an object")
    bad = [name for name in names if not isinstance(found.get(name), str) or not found[name]]
    if bad:
        raise TelemetryError(f"{where} lacks {', '.join(bad)}")
    url = found.get("url", "")
    if "url" in names and not url.startswith(("https://", "http://")):
        raise TelemetryError(f"{where} url is not an http(s) address")
    return {name: str(found[name]).rstrip("/") if name == "url" else str(found[name]) for name in names}


def glitchtip(places: Places) -> GlitchTip:
    keys = read_keys(server_json(places), "glitchtip", ("url", "org", "token"), "the server set-up has not run")
    return GlitchTip(keys["url"], keys["org"], keys["token"])


def langfuse(places: Places) -> Langfuse:
    keys = read_keys(server_json(places), "langfuse", ("url", "public_key", "secret_key"),
                     "the server set-up has not run")  # fmt: skip
    return Langfuse(keys["url"], keys["public_key"], keys["secret_key"])


def _bearer(service: GlitchTip) -> dict[str, str]:
    return {"Authorization": f"Bearer {service.token}"}


def _basic(service: Langfuse) -> dict[str, str]:
    pair = base64.b64encode(f"{service.public_key}:{service.secret_key}".encode()).decode()
    return {"Authorization": f"Basic {pair}"}


def next_cursor(link: str) -> str:
    """The cursor of the next page a Link header names, or "" when there is none. The page's address is built from
    the configured one, never taken from the header, so the token goes nowhere else."""
    for match in _LINK.finditer(link):
        attrs = dict(_ATTR.findall(match.group(1)))
        if attrs.get("rel") == "next" and attrs.get("results") == "true":
            return str(attrs.get("cursor", ""))
    return ""


def _text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return str(value) if isinstance(value, str | int) and not isinstance(value, bool) and value != "" else None


def _issue(raw: Any) -> Issue:
    project = raw.get("project") if isinstance(raw, dict) else None
    slug = project.get("slug") if isinstance(project, dict) else None
    ident = _text(raw, "id") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or ident is None or not isinstance(slug, str) or not slug:
        raise TelemetryError("GlitchTip sent an issue without an id or a project")
    fields = ("title", "culprit", "level", "firstSeen", "lastSeen", "count", "permalink")
    return Issue(ident, slug, *(_text(raw, key) for key in fields))


def unresolved(service: GlitchTip, prefix: str, timeout: float, max_pages: int, want: int | None = None) -> list[Issue]:
    """The unresolved issues of the projects whose slug starts with ``prefix``, newest first, from at most
    ``max_pages`` pages, and at most ``want`` of them when given."""
    found: list[Issue] = []
    cursor = ""
    base = f"{service.url}/api/0/organizations/{urllib.parse.quote(service.org, safe='')}/issues/"
    for _ in range(max_pages):
        query = {"query": "is:unresolved", "sort": "-last_seen", **({"cursor": cursor} if cursor else {})}
        page, headers = get_json("GlitchTip", f"{base}?{urllib.parse.urlencode(query)}", _bearer(service), timeout)
        if not isinstance(page, list):
            raise TelemetryError("GlitchTip's issue list is not a list")
        found += [issue for issue in map(_issue, page) if issue.project.startswith(prefix)]
        if want is not None and len(found) >= want:
            return found[:want]
        cursor = next_cursor(headers.get("link", ""))
        if not cursor:
            break
    return found


def _issue_row(issue: Issue) -> dict[str, Any]:
    return {"id": issue.id, "project": issue.project, "title": issue.title, "culprit": issue.culprit,
            "level": issue.level, "events": issue.count, "first_seen": issue.first_seen,
            "last_seen": issue.last_seen}  # fmt: skip


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TelemetryError("GlitchTip's latest event is not an object")
    exceptions = []
    for entry in _dicts(raw.get("entries")):
        data = entry.get("data")
        if entry.get("type") != "exception" or not isinstance(data, dict):
            continue
        for value in _dicts(data.get("values")):
            trace = value.get("stacktrace")
            frames = [{"file": _text(f, "filename") or _text(f, "absPath"), "function": _text(f, "function"),
                       "line": f.get("lineNo"), "code": _text(f, "context_line"), "in_app": f.get("inApp")}
                      for f in _dicts(trace.get("frames") if isinstance(trace, dict) else None)]  # fmt: skip
            # Innermost first, so a cut to fit drops the outermost calls.
            exceptions.append({"type": _text(value, "type"), "value": _text(value, "value"),
                               "frames_innermost_first": frames[::-1]})  # fmt: skip
    tags = {str(t["key"]): t.get("value") for t in _dicts(raw.get("tags")) if isinstance(t.get("key"), str)}
    return {"title": _text(raw, "title"), "message": _text(raw, "message"), "exceptions": exceptions, "tags": tags}


def _latest(service: GlitchTip, workspace: str, issue_id: Any, timeout: float) -> dict[str, Any]:
    if not isinstance(issue_id, str) or not _ISSUE_ID.match(issue_id):
        raise Refused("args", "--issue is a GlitchTip issue number")
    detail, _ = get_json("GlitchTip", f"{service.url}/api/0/issues/{issue_id}/", _bearer(service), timeout)
    issue = _issue(detail)
    if not issue.project.startswith(f"{workspace}-"):
        raise Refused("telemetry", f"issue {issue_id} is not one of {workspace}'s")
    event, _ = get_json("GlitchTip", f"{service.url}/api/0/issues/{issue_id}/events/latest/", _bearer(service),
                        timeout)  # fmt: skip
    return {"issue": _issue_row(issue), "latest_event": _event(event)}


def _trace_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {"id": _text(raw, "id"), "name": _text(raw, "name"), "at": _text(raw, "timestamp"),
            "latency_seconds": raw.get("latency"), "cost_usd": raw.get("totalCost")}  # fmt: skip


def _recent(service: Langfuse, workspace: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    query = {"environment": workspace, "limit": limit, "orderBy": "timestamp.desc", "fields": "core,metrics"}
    url = f"{service.url}/api/public/traces?{urllib.parse.urlencode(query)}"
    page, _ = get_json("Langfuse", url, _basic(service), timeout)
    rows = page.get("data") if isinstance(page, dict) else None
    if not isinstance(rows, list):
        raise TelemetryError("Langfuse's trace list holds no data")
    return [_trace_row(row) for row in _dicts(rows)]


def _trace(service: Langfuse, workspace: str, trace_id: Any, timeout: float) -> dict[str, Any]:
    if not isinstance(trace_id, str) or not _TRACE_ID.match(trace_id):
        raise Refused("args", "--trace is a Langfuse trace id")
    url = f"{service.url}/api/public/traces/{urllib.parse.quote(trace_id, safe='')}?fields=core,observations,metrics"
    raw, _ = get_json("Langfuse", url, _basic(service), timeout)
    if not isinstance(raw, dict):
        raise TelemetryError("Langfuse's trace is not an object")
    if raw.get("environment") != workspace:
        raise Refused("telemetry", f"trace {trace_id} is not one of {workspace}'s")
    steps = sorted(_dicts(raw.get("observations")), key=lambda o: _text(o, "startTime") or "")
    return {**_trace_row(raw), "observations": [
        {"name": _text(o, "name"), "type": _text(o, "type"), "at": _text(o, "startTime"), "level": _text(o, "level"),
         "status_message": _text(o, "statusMessage"), "model": _text(o, "model"),
         "cost_usd": o.get("calculatedTotalCost")}
        for o in steps
    ]}  # fmt: skip


def workspace_of(ctx: Context, args: dict[str, Any]) -> str:
    """prod or slot<n> from ``--where``; a slot must be slot 0, which is staging, or one of the slots."""
    where = args.get("where")
    if where == PROD:
        return PROD
    if where == SLOT:
        n = args.get("slot")
        slots = [0, *leases.numbers(ctx.places, ctx.settings())]
        if isinstance(n, int) and not isinstance(n, bool) and n in slots:
            return f"slot{n}"
        raise Refused("args", f"--where slot names one of the slots: {', '.join(map(str, slots))}")
    raise Refused("args", "--where is prod or slot <n>; staging is slot 0")


def fit(answer: dict[str, Any], limit: int) -> dict[str, Any]:
    """``answer`` cut to ``limit`` bytes of JSON: from the biggest list or text first, a list losing its last items
    and a text its end; ``trimmed`` says whether anything went."""
    answer["trimmed"] = False
    while (excess := len(json.dumps(answer).encode()) - limit) > 0:
        answer["trimmed"] = True
        places = [(holder, key) for holder, key in _places(answer) if _cuttable(holder[key])]
        if not places:
            break
        holder, key = max(places, key=lambda place: len(json.dumps(place[0][place[1]]).encode()))
        value = holder[key]
        if isinstance(value, str):
            holder[key] = value[: max(0, len(value) - excess - 1)] + "…"
            continue
        freed = 0
        while len(value) > 1 and freed < excess:
            freed += len(json.dumps(value.pop()).encode()) + 2
    return answer


def _cuttable(value: Any) -> bool:
    """A text, or a list of two or more items; a list of one is cut inside its item instead."""
    return (isinstance(value, str) and len(value) > 1) or (isinstance(value, list) and len(value) > 1)


def _places(node: Any) -> list[tuple[Any, Any]]:
    """Every (container, key or index) that holds a value inside ``node``."""
    found: list[tuple[Any, Any]] = []
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in items:
        found.append((node, key))
        found += _places(value)
    return found


def errors(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory errors`: a workspace's unresolved GlitchTip issues, newest first, or one issue and its latest event."""
    settings = ctx.settings()
    workspace = workspace_of(ctx, args)
    timeout = float(settings.get_int("telemetry.timeout_seconds"))
    try:
        service = glitchtip(ctx.places)
        if "issue" in args:
            answer = {"where": workspace, **_latest(service, workspace, args["issue"], timeout)}
        else:
            found = unresolved(service, f"{workspace}-", timeout, settings.get_int("errors.max_pages"),
                               settings.get_int("telemetry.max_items"))  # fmt: skip
            answer = {"where": workspace, "issues": [_issue_row(issue) for issue in found]}
    except TelemetryError as exc:
        raise Refused("telemetry", str(exc)) from exc
    return fit(ok(**answer), settings.get_int("telemetry.max_kib") * 1024)


def traces(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory traces`: a workspace's recent Langfuse traces, newest first, or one trace with its steps."""
    settings = ctx.settings()
    workspace = workspace_of(ctx, args)
    timeout = float(settings.get_int("telemetry.timeout_seconds"))
    try:
        service = langfuse(ctx.places)
        answer: dict[str, Any] = {"where": workspace}
        if "trace" in args:
            answer["trace"] = _trace(service, workspace, args["trace"], timeout)
        else:
            answer["traces"] = _recent(service, workspace, settings.get_int("telemetry.max_items"), timeout)
    except TelemetryError as exc:
        raise Refused("telemetry", str(exc)) from exc
    return fit(ok(**answer), settings.get_int("telemetry.max_kib") * 1024)
