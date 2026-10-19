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

from runtime.queue import approvals, formats, reports, turn
from runtime.queue.book import Queue
from runtime.shared import claude_accounts, store
from runtime.shared.models import SEVERITIES
from runtime.slots import leases
from runtime.system import layout
from runtime.warden import gc, imports, loop
from runtime.warden.context import Context
from runtime.warden.health import errors_import
from runtime.warden.owner import reflections
from runtime.warden.sessions import older_skills, registry

log = logging.getLogger(__name__)
# The first line of an issue's request body as glitchtip files it (errors_import.request_text).
_ISSUE = re.compile(r"^GlitchTip issue (\S+) in (\S+) ")
TYPED = "typed"  # the agent of a finding carried from a verdict a tester typed


def migrate(ctx: Context) -> list[str]:
    """Bring every old shape over; what each step did."""
    done: list[str] = []
    steps: list[Callable[[Context], str]] = [
        _inbox_cursor, _leases, _errors_filed, _errors_since, _failing_since, _turn, _merges, _staging_red,
        _typed_verdicts, _approvals_routed, _briefs_home, _active_account, _older_skills, _reflection_states,
        _gc_record,
    ]  # fmt: skip
    for step in steps:
        try:
            said = step(ctx)
        except Exception as exc:
            log.exception("warden: bringing the state over failed at %s (%s)", step.__name__, type(exc).__name__)
            continue
        if said:
            log.info("warden: brought the state over: %s", said)
            done.append(said)
    return done


def _gc_record(ctx: Context) -> str:
    """A clean-up record that does not say what failed goes, and the Docker reading: the clean-up writes its own."""
    docker, raw = ctx.places.state / "docker-disk.json", gc.record(ctx.places)
    old = raw is not None and "failed" not in raw
    if not old and not docker.exists():
        return ""
    docker.unlink(missing_ok=True)
    if old:
        gc.record_path(ctx.places).unlink()
    return "the clean-up's records"


def _reflection_states(ctx: Context) -> str:
    """A Self-reflect entry with no state opens, under the id the Library gave it by its place."""
    raw = store.read_json_or(reflections.records_path(ctx.places), [])
    old = [n for n, e in enumerate(raw, 1) if isinstance(e, dict) and "state" not in e] if isinstance(raw, list) else []
    for n in old:
        raw[n - 1].update(id=f"{reflections.ID_PREFIX}{n}", state=reflections.OPEN, closed=None, note="",
                          came_back=False)  # fmt: skip
    if not old:
        return ""
    reflections.save(ctx.places, raw)
    reflections.entries(ctx.places)
    return f"{len(old)} Self-reflect entries, each open"


def _older_skills(ctx: Context) -> str:
    found = older_skills.note_install(ctx.places)
    return f"{found} sessions load their skills again as they come back" if found else ""


# A spec's or card's brief that was a Claude artifact; a brief now lives on this machine, under its version.
_ARTIFACT_BRIEF = re.compile(r"(?m)^brief: https://claude\.ai/\S*$")


def _briefs_home(ctx: Context) -> str:
    """A spec or card whose brief is a Claude artifact names no brief, and a card drops the comment count its
    designer kept: the brief's own threads count now."""
    queue = Queue.of(ctx)
    specs = 0
    with queue.lock():
        for name in queue.files.names("specs"):
            rel = f"specs/{name}"
            text = queue.files.read(rel).decode()
            if _ARTIFACT_BRIEF.search(text):
                queue.write({rel: _ARTIFACT_BRIEF.sub("brief:", text)})
                specs += 1
    raw = store.read_json_or(ctx.places.cards, [])
    old = [
        c for c in raw if isinstance(c, dict) and ("comments" in c or str(c.get("brief", "")).startswith("https://"))
    ]
    for card in old:
        card.pop("comments", None)
        card.pop("comments_at", None)
        card["brief"] = "" if str(card.get("brief", "")).startswith("https://") else card.get("brief", "")
    if old:
        store.write_json(ctx.places.cards, raw, mode=0o600)
    return f"{specs} specs and {len(old)} cards off their artifact briefs" if specs or old else ""


def _active_account(ctx: Context) -> str:
    """The file that named the active Claude account goes: the active one is whichever the agents' Claude holds."""
    path = claude_accounts.folder(layout.secrets(ctx.places.state)) / "active"
    if not path.exists():
        return ""
    path.unlink()
    return "the active account's file, read from the agents' sign-in now"


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


def _errors_since(ctx: Context) -> str:
    """An errors record that has read before reads on from now; one that never read takes what is open as known."""
    changed = []
    for env in errors_import.ENVS:
        raw = store.read_json_or(errors_import.path(ctx.places, env), None)
        if isinstance(raw, dict) and "since" not in raw:
            since = ctx.clock.iso() if raw.get("polled") is not None else None
            store.write_json(errors_import.path(ctx.places, env), {**raw, "since": since})
            changed.append(env)
    return f"when the {', '.join(changed)} errors imports began" if changed else ""


def _failing_since(ctx: Context) -> str:
    """An import's timer that said only whether it was failing says since when: from now, the first time known."""
    changed = []
    for path in (reports.timer_path(ctx.places), *(errors_import.path(ctx.places, env) for env in errors_import.ENVS)):
        raw = store.read_json_or(path, None)
        if isinstance(raw, dict) and "failing" in raw:
            failing = raw.pop("failing")
            store.write_json(path, {**raw, "failing_since": ctx.clock.now() if failing else None})
            changed.append(path.name)
    return f"since when {', '.join(changed)} have failed" if changed else ""


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


def _staging_red(ctx: Context) -> str:
    """The loop's memo drops the request it kept for staging's red spell: staging is slot 0, which never goes red."""
    raw = store.read_json_or(loop.memo_path(ctx.places), None)
    if not isinstance(raw, dict) or "staging_red" not in raw:
        return ""
    del raw["staging_red"]
    store.write_json(loop.memo_path(ctx.places), raw)
    return "the loop's memo"


def _typed_verdicts(ctx: Context) -> str:
    """A verdict a tester typed (it names no agent) becomes findings of its counts, so the QA gate still sees what it
    found open."""
    sessions = registry.read(ctx.places)
    carried = 0
    for path in sorted(ctx.places.qa_results.glob("*.jsonl")):
        session = sessions.get(path.stem)
        rows = store.read_jsonl(path)
        typed = [r for r in rows if "agent" not in r]
        if session is None or not session.task or not typed:
            continue
        for row in typed:
            for severity, key in ((s, f"{s}s") for s in SEVERITIES):
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
