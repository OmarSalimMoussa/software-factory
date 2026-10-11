"""What an install of an earlier factory left in the warden's state, brought to what this one reads.

The warden runs it once at its start, before any pass or request reads the state. Each step looks for the shape an
earlier factory wrote and leaves any other alone, so on a state already brought over it changes nothing. A step that
fails is logged and the others still run: the reader it serves then refuses loudly, naming its file.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from runtime.queue import approvals, formats, turn
from runtime.queue.book import Queue
from runtime.shared import store
from runtime.slots import leases
from runtime.warden import imports
from runtime.warden.context import Context
from runtime.warden.delivery import deploys, github_push
from runtime.warden.health import errors_import, probes
from runtime.warden.sessions import registry

log = logging.getLogger(__name__)
# The first line of an issue's request body as glitchtip files it (errors_import.request_text).
_ISSUE = re.compile(r"^GlitchTip issue (\S+) in (\S+) ")
TYPED = "typed"  # the agent of a finding carried from a verdict a tester typed


def migrate(ctx: Context) -> list[str]:
    """Bring every old shape over; what each step did."""
    done: list[str] = []
    steps: list[Callable[[Context], str]] = [
        _inbox_cursor, _leases, _errors_filed, _turn, _merges, _released, _typed_verdicts, _approvals_routed,
    ]  # fmt: skip
    for step in steps:
        try:
            said = step(ctx)
        # telemetry: last-resort — logged with its trace; the reader of that state then refuses, naming the file
        except Exception as exc:
            log.exception("warden: bringing the state over failed at %s (%s)", step.__name__, type(exc).__name__)
            continue
        if said:
            log.info("warden: brought the state over: %s", said)
            done.append(said)
    return done


def _inbox_cursor(ctx: Context) -> str:
    """One count of events delivered becomes the handed and the acknowledged counts: what was delivered was acted on."""
    path = ctx.places.state / "inbox-cursor.json"
    raw = store.read_json_or(path, None)
    if not isinstance(raw, dict) or "delivered" not in raw or not isinstance(raw["delivered"], int):
        return ""
    store.write_json(path, {"handed": raw["delivered"], "acked": raw["delivered"], "at": ctx.clock.iso()}, mode=0o600)
    return "the inbox cursor"


def _leases(ctx: Context) -> str:
    """A slot lease that still names the holder's checkout drops it."""
    changed = []
    for path in sorted(ctx.places.slots.glob("*/lease")):
        lines = path.read_text().splitlines()
        if lines and lines[0].startswith("checkout="):
            store.write_text(path, "".join(f"{line}\n" for line in lines[1:]), mode=0o600)
            changed.append(path.parent.name)
    if changed:
        leases.read(ctx.places, int(changed[0]))  # the new shape reads, or this raises
    return f"slot leases {', '.join(changed)}" if changed else ""


def _errors_filed(ctx: Context) -> str:
    """Each environment's record of the issues it filed, rebuilt from the requests glitchtip filed: the old record
    kept prod's ids alone, without their requests, so a fix that ships could never be told from a come-back."""
    old = ctx.places.state / "errors-seen.json"
    if not old.exists() or any(errors_import.path(ctx.places, env).exists() for env in errors_import.ENVS):
        return ""
    requests, _ = Queue.of(ctx).requests()
    filed: dict[str, dict[str, Any]] = {env: {} for env in errors_import.ENVS}
    for req in sorted(requests, key=lambda r: r.at):
        found = _ISSUE.match(req.body) if req.from_role == errors_import.SENDER else None
        env = found.group(2).split("-", 1)[0] if found else ""
        if found and env in filed:
            filed[env][found.group(1)] = {"request": req.id, "at": req.at}
    for env, issues in filed.items():
        store.write_json(errors_import.path(ctx.places, env), {"filed": issues, **imports.fresh()})
    old.unlink()
    return "the errors imports' records"


def _turn(ctx: Context) -> str:
    """A task the old factory left in its merge turn holds it here too, so its merge can land."""
    if turn.holder(ctx.places) is not None:
        return ""
    holding = [t for t in Queue.of(ctx).tasks()[0] if t.status == "turn"]
    if len(holding) != 1:
        return ""
    with ctx.lock:
        turn.held(ctx, holding[0])
    return f"the merge turn of {holding[0].id}"


def _merges(ctx: Context) -> str:
    """Every merge the old factory logged for a task, by its full commit, so a merge not yet pushed still ships."""
    if turn.merges_path(ctx.places).exists():
        return ""
    queue = Queue.of(ctx)
    repo = ctx.settings().get_str("repo.path")
    found: dict[str, str] = {}
    for task in queue.tasks()[0]:
        for entry in queue.read_log(task.id):
            if entry["event"] != "merged" or not formats.SHA.match(entry.get("commit", "")):
                continue
            code, full = ctx.launcher.agent_command(
                ["git", "-C", repo, "rev-parse", f"{entry['commit']}^{{commit}}"], repo
            )
            if code == 0:
                found[full.strip()] = task.id
    store.write_json(turn.merges_path(ctx.places), found, mode=0o600)
    return f"{len(found)} merges"


def _released(ctx: Context) -> str:
    """The commit the last releases ran at: prod's current build, which the old factory released against."""
    if github_push.released(ctx.places) is not None:
        return ""
    current = deploys.builds(ctx.places, probes.PROD)["current"]
    if current is None:
        return ""
    store.write_json(github_push.released_path(ctx.places), {"commit": current["commit"], "at": ctx.clock.iso()})
    return "the last released commit"


def _typed_verdicts(ctx: Context) -> str:
    """A verdict a tester typed (it names no agent) becomes findings of its counts, so qa-clean still sees what it
    found open."""
    sessions = registry.read(ctx.places)
    carried = 0
    for path in sorted((ctx.places.state / "qa-results").glob("*.jsonl")):
        session = sessions.get(path.stem)
        rows = store.read_jsonl(path)
        typed = [r for r in rows if "agent" not in r]
        if session is None or not session.task or not typed:
            continue
        for row in typed:
            for severity, key in (("blocker", "blockers"), ("major", "majors"), ("minor", "minors")):
                for _ in range(int(row.get(key) or 0)):
                    carried += 1
                    _finding(ctx, session.task, row, severity)
        store.write_text(path, "".join(json.dumps({"agent": TYPED, **r}, sort_keys=True) + "\n" for r in rows),
                         mode=0o600)  # fmt: skip
    return f"{carried} typed findings" if carried else ""


def _approvals_routed(ctx: Context) -> str:
    """An approval names the rung its task's facts routed to; one made before took its approved rung for it, the
    ceiling the old factory held a start to."""
    carried = []
    for path in sorted(ctx.places.approvals.glob("*.json")):
        raw = store.read_json(path)
        if path.name.endswith(".go.json") or not isinstance(raw, dict) or "routed" in raw or "rung" not in raw:
            continue
        store.write_json(path, {**raw, "routed": raw["rung"]}, mode=0o600)
        approvals.current(ctx.places, path.stem)  # the new shape reads, or this raises
        carried.append(path.stem)
    return f"the routes of approvals {', '.join(carried)}" if carried else ""


def _finding(ctx: Context, task_id: str, row: dict[str, Any], severity: str) -> None:
    path = ctx.places.state / "qa-findings" / f"{task_id}.jsonl"
    number = sum(1 for r in store.read_jsonl(path) if "finding" in r) + 1
    store.append_jsonl(path, {"finding": f"f-{number}", "head": row["head"], "tester": row["tester"],
                              "agent": TYPED, "severity": severity, "where": "a verdict typed before the upgrade",
                              "text": "Counted from the verdict this tester typed; its words were not kept.",
                              "at": row.get("at", ctx.clock.iso())})  # fmt: skip
