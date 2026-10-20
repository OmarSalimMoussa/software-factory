"""`factory errors`, read-only for agents, and the GlitchTip reads the errors import and `factory logs` share.

The warden holds every key and asks GlitchTip itself; an agent sees only the answer. Every workspace, prod and each
slot (slot 0 is staging), lives on this machine's shared services, reached with the keys in the secrets' server.json
that the server set-up wrote. A workspace's GlitchTip projects are named for it (prod-…, slot0-…, slot3-…), so a read
never strays into another workspace, or into the factory's own project. Every answer is cut to telemetry.max_kib. A
key never appears in an answer, an error or a log line.

GlitchTip's issue search matches whole words, and its time filter reads when an issue was first seen. So the warden
narrows each page itself: `--find` keeps an issue whose title or culprit holds the text in any case, and `--since`
one last seen from then on. The list is newest seen first, so the first issue seen before `--since` ends the read.
"""

from __future__ import annotations

import itertools
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
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
_LINK = re.compile(r'<[^>]*>((?:\s*;\s*[A-Za-z]+="[^"]*")*)')
_ATTR = re.compile(r'([A-Za-z]+)="([^"]*)"')


class TelemetryError(Exception):
    """GlitchTip or the server's keys could not be read; the message says which and why, never a key."""


class NotSetUp(TelemetryError):
    """The keys that reach a service are not in place yet; the message says which set-up has not run."""


@dataclass(frozen=True, slots=True)
class GlitchTip:
    url: str
    org: str
    token: str = field(repr=False)


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


def bearer(service: GlitchTip) -> dict[str, str]:
    return {"Authorization": f"Bearer {service.token}"}


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


def unresolved(service: GlitchTip, prefix: str, timeout: float, max_pages: int, want: int | None = None, *,
               find: str = "", since: datetime | None = None) -> list[Issue]:  # fmt: skip
    """The unresolved issues of the projects whose slug starts with ``prefix``, newest first, from at most
    ``max_pages`` pages, and at most ``want`` of them when given; only those whose title or culprit holds ``find``,
    in any case, and those last seen from ``since`` on."""
    found: list[Issue] = []
    cursor = ""
    base = f"{service.url}/api/0/organizations/{urllib.parse.quote(service.org, safe='')}/issues/"
    for _ in range(max_pages):
        query = {"query": "is:unresolved", "sort": "-last_seen", **({"cursor": cursor} if cursor else {})}
        page, headers = get_json("GlitchTip", f"{base}?{urllib.parse.urlencode(query)}", bearer(service), timeout)
        if not isinstance(page, list):
            raise TelemetryError("GlitchTip's issue list is not a list")
        issues = [_issue(raw) for raw in page]
        recent = list(itertools.takewhile(lambda issue: since is None or _last_seen(issue) >= since, issues))
        found += [issue for issue in recent if issue.project.startswith(prefix) and _holds(issue, find)]
        if want is not None and len(found) >= want:
            return found[:want]
        cursor = next_cursor(headers.get("link", ""))
        if not cursor or len(recent) < len(issues):
            break
    return found


def _last_seen(issue: Issue) -> datetime:
    try:
        seen = datetime.fromisoformat(issue.last_seen or "")
    except ValueError as exc:
        raise TelemetryError(f"GlitchTip sent issue {issue.id} without a time it was last seen") from exc
    if seen.tzinfo is None:
        raise TelemetryError(f"GlitchTip sent issue {issue.id}'s last seen time without its zone")
    return seen


def _holds(issue: Issue, find: str) -> bool:
    texts = (text.casefold() for text in (issue.title, issue.culprit) if text is not None)
    return not find or any(find.casefold() in text for text in texts)


def find_of(args: dict[str, Any]) -> str:
    if "find" in args and (not isinstance(args["find"], str) or not args["find"].strip()):
        raise Refused("args", "--find is the text to search for")
    return str(args.get("find", ""))


def since_of(args: dict[str, Any]) -> datetime | None:
    if "since" not in args:
        return None
    try:
        since = datetime.fromisoformat(args["since"]) if isinstance(args["since"], str) else None
    # telemetry: expected — the agent's own input, refused below with the form it takes
    except ValueError:
        since = None
    if since is None or since.tzinfo is None:
        raise Refused("args", "--since is a time with its zone, such as 2026-10-17T09:17:00Z")
    return since


def _issue_row(issue: Issue) -> dict[str, Any]:
    return {"id": issue.id, "project": issue.project, "title": issue.title, "culprit": issue.culprit,
            "level": issue.level, "events": issue.count, "first_seen": issue.first_seen,
            "last_seen": issue.last_seen}  # fmt: skip


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _event(raw: dict[str, Any]) -> dict[str, Any]:
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
    request = next((e.get("data") for e in _dicts(raw.get("entries")) if e.get("type") == "request"), None)
    return {"title": _text(raw, "title"), "message": _text(raw, "message"), "exceptions": exceptions, "tags": tags,
            "request": request}  # fmt: skip


def _latest(service: GlitchTip, workspace: str, issue_id: Any, timeout: float, whole: bool) -> dict[str, Any]:
    if not isinstance(issue_id, str) or not _ISSUE_ID.match(issue_id):
        raise Refused("args", "--issue is a GlitchTip issue number")
    detail, _ = get_json("GlitchTip", f"{service.url}/api/0/issues/{issue_id}/", bearer(service), timeout)
    issue = _issue(detail)
    if not issue.project.startswith(f"{workspace}-"):
        raise Refused("telemetry", f"issue {issue_id} is not one of {workspace}'s")
    event, _ = get_json("GlitchTip", f"{service.url}/api/0/issues/{issue_id}/events/latest/", bearer(service),
                        timeout)  # fmt: skip
    if not isinstance(event, dict):
        raise TelemetryError("GlitchTip's latest event is not an object")
    return {"issue": _issue_row(issue), "latest_event": event if whole else _event(event)}


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
    """`factory errors`: a workspace's unresolved GlitchTip issues, newest first, those holding ``find`` and seen
    since ``since`` when given, or one issue and its latest event, whole when asked."""
    settings = ctx.settings()
    workspace = workspace_of(ctx, args)
    find, since = find_of(args), since_of(args)
    if "issue" in args and (find or since):
        raise Refused("args", "--find and --since narrow the list of issues; --issue reads one issue")
    timeout = float(settings.get_int("telemetry.timeout_seconds"))
    try:
        service = glitchtip(ctx.places)
        if "issue" in args:
            answer = {"where": workspace, **_latest(service, workspace, args["issue"], timeout, "whole" in args)}
        elif "whole" in args:
            raise Refused("args", "--whole reads one issue's newest event: name the issue with --issue")
        else:
            found = unresolved(service, f"{workspace}-", timeout, settings.get_int("errors.max_pages"),
                               settings.get_int("telemetry.max_items"), find=find, since=since)  # fmt: skip
            answer = {"where": workspace, "issues": [_issue_row(issue) for issue in found]}
    except TelemetryError as exc:
        raise Refused("telemetry", str(exc)) from exc
    return fit(ok(**answer), settings.get_int("telemetry.max_kib") * 1024)
