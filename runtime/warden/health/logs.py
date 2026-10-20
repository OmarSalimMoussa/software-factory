"""`factory logs`: a workspace's log lines in the shared GlitchTip, newest first, read with the warden's key.

A workspace's lines are those its own projects sent (prod-…, slot0-…), so a read never strays into another
workspace's: with no project of its own, a workspace has no lines and GlitchTip is never asked for any. `--find` is
GlitchTip's own search, which reads a line's text and not its attributes, so every line comes with its attributes.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.health import telemetry
from runtime.warden.health.telemetry import GlitchTip, TelemetryError


def logs(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory logs`: the newest lines, those whose text holds ``find`` and from ``since`` when given; ``more`` says
    whether older ones match too."""
    settings = ctx.settings()
    workspace = telemetry.workspace_of(ctx, args)
    query = _query(args)
    timeout = float(settings.get_int("telemetry.timeout_seconds"))
    try:
        service = telemetry.glitchtip(ctx.places)
        projects = _projects(service, f"{workspace}-", timeout, settings.get_int("errors.max_pages"))
        lines, more = _lines(service, projects, query, settings.get_int("telemetry.max_items"), timeout)
    except TelemetryError as exc:
        raise Refused("telemetry", str(exc)) from exc
    return telemetry.fit(ok(where=workspace, lines=lines, more=more), settings.get_int("telemetry.max_kib") * 1024)


def _query(args: dict[str, Any]) -> dict[str, str]:
    find, since = telemetry.find_of(args), telemetry.since_of(args)
    return {**({"query": find} if find else {}), **({"start": since.isoformat()} if since else {})}


def _org(service: GlitchTip) -> str:
    return f"{service.url}/api/0/organizations/{urllib.parse.quote(service.org, safe='')}"


def _projects(service: GlitchTip, prefix: str, timeout: float, max_pages: int) -> dict[str, str]:
    """Each project's slug by its id, of those whose slug starts with ``prefix``; no page of them may be left out."""
    found: dict[str, str] = {}
    cursor = ""
    for _ in range(max_pages):
        query = f"?{urllib.parse.urlencode({'cursor': cursor})}" if cursor else ""
        page, headers = telemetry.get_json("GlitchTip", f"{_org(service)}/projects/{query}", telemetry.bearer(service),
                                           timeout)  # fmt: skip
        if not isinstance(page, list):
            raise TelemetryError("GlitchTip's project list is not a list")
        for project in page:
            ident, slug = (project.get("id"), project.get("slug")) if isinstance(project, dict) else (None, None)
            if not isinstance(ident, str | int) or isinstance(ident, bool) or not isinstance(slug, str):
                raise TelemetryError("GlitchTip sent a project without an id or a slug")
            if slug.startswith(prefix):
                found[str(ident)] = slug
        cursor = telemetry.next_cursor(headers.get("link", ""))
        if not cursor:
            return found
    raise TelemetryError(f"GlitchTip lists more than {max_pages} pages of projects")


def _lines(service: GlitchTip, projects: dict[str, str], query: dict[str, str], limit: int,
           timeout: float) -> tuple[list[dict[str, Any]], bool]:  # fmt: skip
    if not projects:
        return [], False
    params = urllib.parse.urlencode({"project": list(projects), **query, "limit": limit}, doseq=True)
    page, headers = telemetry.get_json("GlitchTip", f"{_org(service)}/logs/?{params}", telemetry.bearer(service),
                                       timeout)  # fmt: skip
    if not isinstance(page, list):
        raise TelemetryError("GlitchTip's log list is not a list")
    lines = []
    for raw in page:
        project = projects.get(str(raw.get("projectId"))) if isinstance(raw, dict) else None
        if project is None:
            raise TelemetryError("GlitchTip sent a log line from a project it was not asked for")
        lines.append({"at": raw.get("timestamp"), "project": project, "level": raw.get("level"),
                      "text": raw.get("body"), "attributes": raw.get("data")})  # fmt: skip
    return lines, bool(telemetry.next_cursor(headers.get("link", "")))
