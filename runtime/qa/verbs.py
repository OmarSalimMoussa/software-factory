"""The QA verbs (factory spec, "QA"): `factory qa plan`, `qa-ready`, `ready-check`, `qa step`, `qa step-result`,
`qa result` and `qa-clean`.

The warden reads the branch with git as the agent, through the agent's own unit, and reads surfaces.toml from the
default branch, which only the owner changes, never from the worktree. A ready record names the full head the slot
runs; the guard reads the records from public/ to let testers start, and counts QA rounds by their heads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.guard.facts import REPRODUCE
from runtime.qa import evidence, findings, plan, qa_browser, steps
from runtime.qa.widths import unwalked
from runtime.queue import tasks
from runtime.queue.formats import SHA, one_line
from runtime.queue.statuses import IMPL
from runtime.shared import lanes, slices, store
from runtime.shared.models import RETIRED, TESTER, Session, Task
from runtime.shared.paths import scratch_of
from runtime.shared.protocol import Refused, ok
from runtime.slots import health, leases
from runtime.warden.context import Context
from runtime.warden.sessions import doing, registry

SURFACES_FILE = "check-values/surfaces.toml"


def _implementer(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.role != IMPL or session.state == RETIRED:
        raise Refused("qa", "QA belongs to a live implementer's run")
    return session


def _git(ctx: Context, cwd: str, *argv: str) -> str:
    code, out = ctx.launcher.agent_command(["git", "-C", cwd, *argv], cwd)
    if code != 0:
        raise Refused("git", f"git {argv[0]} exited {code}: {out.strip()[-200:]}")
    return out.strip()


def _head(ctx: Context, session: Session) -> str:
    return _git(ctx, session.worktree, "rev-parse", "HEAD")


def _ready_path(ctx: Context, session: Session) -> Path:
    return ctx.places.ready(session.id)


def ready_records(ctx: Context, session: Session) -> list[dict[str, Any]]:
    raw = store.read_json_or(_ready_path(ctx, session), {"records": []})
    records = raw.get("records") if isinstance(raw, dict) else None
    if not isinstance(records, list):
        raise store.StoreError(f"{_ready_path(ctx, session)}: no records list")
    return records


def _results_path(ctx: Context, session: Session) -> Path:
    return ctx.places.results_of(session.id)


def _diff_surfaces(ctx: Context, session: Session, *span: str) -> dict[str, list[str]]:
    """Each surface the worktree's diff over ``span`` touches, with the paths that touch it."""
    settings = ctx.settings()
    surfaces = f"{settings.get_str('repo.default_branch')}:{SURFACES_FILE}"
    prefixes = plan.surface_prefixes(_git(ctx, settings.get_str("repo.path"), "show", surfaces))
    changed = _git(ctx, session.worktree, "diff", "--name-only", "--no-renames", *span).splitlines()
    return plan.diff_surfaces(changed, prefixes)


def testers_for(ctx: Context, session: Session, task: Task) -> tuple[dict[str, list[str]], list[int]]:
    """The required testers for the worktree's head, each with why, by the task's surfaces and the diff's joined; and
    the widths their walks show when those surfaces hold the screens."""
    settings = ctx.settings()
    diff = _diff_surfaces(ctx, session, f"{settings.get_str('repo.default_branch')}...HEAD")
    rung = task.run if task.run is not None else session.rung
    if rung is None:
        raise Refused("qa", f"{task.id} has no run, so its testers are unknown")
    screens = plan.SCREENS in [*task.surfaces, *diff]
    return plan.required(task.surfaces, rung, diff), [int(w) for w in settings.get_list("qa.widths")] if screens else []


def qa_plan(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    session = _implementer(ctx)
    found, _ = testers_for(ctx, session, tasks.read(ctx, session.task))
    return ok(head=_head(ctx, session), testers=[{"tester": t, "why": why} for t, why in found.items()])


def qa_ready(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Every slice is built and the slot runs the head: testers may start on it. The task records the worktree's
    branch here: its merge turn names it, and the warden finds its merge by it. A reproduction (`_unbuilt`) shows a
    bug before its fix, in no QA round, and no result of its testers counts."""
    session = _implementer(ctx)
    asked, slices, reproduce = args.get("head"), args.get("slices"), args.get("reproduce") is True
    if not isinstance(asked, str) or not SHA.match(asked):
        raise Refused("qa-ready", "qa-ready names the head: qa-ready <head>")
    task = tasks.read(ctx, session.task)
    slices = _unbuilt(ctx, session, asked, slices) if reproduce else _ready_slices(ctx, session, asked, task, slices)
    head = _head(ctx, session)
    if not head.startswith(asked):
        raise Refused("qa-ready", f"{asked[:9]} is not your branch's head ({head[:9]}): commit, then ask again")
    branch = _git(ctx, session.worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise Refused("qa-ready", "your worktree is on no branch: commit on your own branch, then ask again")
    for commit in slices:
        code, _ = ctx.launcher.agent_command(
            ["git", "-C", session.worktree, "merge-base", "--is-ancestor", commit, head], session.worktree
        )
        if code != 0:
            raise Refused("qa-ready", f"{commit[:9]} is not on your branch inside {head[:9]}")
    lease = leases.held_by(ctx.places, ctx.settings(), session.name)
    if lease is None:
        raise Refused("slot", "claim a slot and rebuild it at your head first: factory slot claim, then slot up")
    why = health.off_head(ctx, lease.slot, session.id, head)
    if why is not None:
        raise Refused("qa-ready", why)
    testers, widths = testers_for(ctx, session, task)
    marks: dict[str, Any] = {REPRODUCE: True} if reproduce else {lanes.WIDTHS: widths} if widths else {}
    with ctx.lock:
        records = ready_records(ctx, session)
        record = {"head": head, "slices": slices, "at": ctx.clock.iso(), "slot": lease.slot, "testers": list(testers),
                  **marks}  # fmt: skip
        store.write_json(_ready_path(ctx, session), {"records": [*records, record]})
        rounds = len({str(r["head"]) for r in [*records, record] if not r.get(REPRODUCE)})
        if not reproduce:
            doing.record(ctx.places, session.id, slice=len(slices), slices=len(slices), qa_round=rounds)
    if task.status == "building" and not reproduce:
        tasks.move(ctx, task.id, "qa", "qa-ready", IMPL, f"ready for QA at {head[:9]}", branch=branch)
    return ok(head=head, slot=lease.slot, testers=[{"tester": t, "why": why} for t, why in testers.items()])


def _unbuilt(ctx: Context, session: Session, asked: str, slices: Any) -> list[str]:
    """A reproduction's slices: none, on a branch whose commits change no file but Markdown, such as its spec."""
    default = ctx.settings().get_str("repo.default_branch")
    changed = _git(ctx, session.worktree, "log", "--format=", "--name-only", f"{default}..{asked}").splitlines()
    if slices is not None or not all(path.endswith(".md") for path in changed if path):
        raise Refused("qa-ready", "a reproduction comes before your first commit beyond Markdown, such as your spec, "
                                  "and names no slice; once the fix is built, run qa-ready on its head")  # fmt: skip
    return []


def _ready_slices(ctx: Context, session: Session, asked: str, task: Task, slices: Any) -> list[str]:
    """The commits qa-ready names, or the slices' own on the branch when it names none: one for each slice."""
    if slices is None:
        slices = _slice_commits(ctx, session, asked, task.slices)
    if not isinstance(slices, list) or not slices or not all(isinstance(s, str) and SHA.match(s) for s in slices):
        raise Refused("qa-ready", "each slice is a commit sha")
    if task.slices and len(slices) != task.slices:
        raise Refused("qa-ready", f"{task.id} has {task.slices} slices; name one commit for each, not {len(slices)}")
    return slices


def _slice_commits(ctx: Context, session: Session, head: str, count: int | None) -> list[str]:
    """The commit each slice ended with, found by its `Slice: <n>/<N>` line on the branch: the newest per slice, in
    slice order. Refused, naming what is wrong, unless every slice of the task's count has one."""
    default = ctx.settings().get_str("repo.default_branch")
    # One record per commit: the sha, a unit separator, the whole message, a record separator.
    log = _git(ctx, session.worktree, "log", "--format=%H%x1f%B%x1e", f"{default}..{head}")
    found: dict[int, str] = {}
    for record in log.split("\x1e"):
        commit, _, message = record.strip().partition("\x1f")
        try:
            place = slices.mark(message)
        except slices.SliceError as exc:
            raise Refused("qa-ready", f"{commit[:9]} cannot be read as one slice: {exc}") from None
        if place is None:
            continue
        if count and place[1] != count:
            raise Refused("qa-ready", f"{commit[:9]} says Slice: {place[0]}/{place[1]}, but the task has {count} "
                                      "slices; name the commits with --slices")  # fmt: skip
        found.setdefault(place[0], commit)
    want = count if count else max(found, default=0)
    missing = [str(n) for n in range(1, want + 1) if n not in found]
    if not want or missing:
        named = f"Slice: {', '.join(missing) or 1}/{want or 'N'}"
        raise Refused("qa-ready", f"no commit on your branch has the line {named}; end each slice's commit with it, "
                                  "or name the commits with --slices")  # fmt: skip
    return [found[n] for n in range(1, want + 1)]


def _touched(ctx: Context, session: Session, task: Task, tested: str, head: str) -> set[str]:
    """The testers the commits from ``tested`` to ``head`` require: those whose surfaces they touch."""
    rung = task.run if task.run is not None else session.rung
    return set(plan.required([], rung or 0, _diff_surfaces(ctx, session, tested, head)))


def ready_check(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester's first step: the last ready record still holds, the slot runs its head; the tester's folder, and the
    services a set-up stopped."""
    session = _implementer(ctx)
    records = ready_records(ctx, session)
    if not records:
        raise Refused("ready-check", "not ready: no slices recorded")
    latest = records[-1]
    head = str(latest["head"])
    why = health.off_head(ctx, int(latest["slot"]), session.id, head)
    if why is not None:
        raise Refused("ready-check", f"not ready: {why}")
    folder = {"folder": str(Path(session.worktree) / scratch_of(ctx.caller.agent_id))} if ctx.caller.agent_id else {}
    noted = health.stopped(ctx.places, int(latest["slot"]))
    stopped = {"stopped": f"{', '.join(noted.services)}, by your implementer for a set-up: no fault"} if noted else {}
    return ok(head=head, slot=latest["slot"], testers=latest.get("testers", []), **folder, **stopped)


def _tester_on_head(ctx: Context, args: dict[str, Any], verb: str) -> tuple[Session, Session, str]:
    """The implementer, its registered tester that asks, and the full head of the last ready record it names."""
    session = _implementer(ctx)
    tester = registry.read(ctx.places).get(ctx.caller.agent_id) if ctx.caller.agent_id else None
    if tester is None or not tester.role.startswith(TESTER):
        raise Refused(verb, "only a registered tester reports a QA finding or result")
    head = args.get("head")
    records = ready_records(ctx, session)
    if not isinstance(head, str) or not records or not str(records[-1]["head"]).startswith(head) or len(head) < 7:
        raise Refused(verb, "report on the head of the last ready record: --head <sha>")
    if any(r.get("agent") == ctx.caller.agent_id for r in store.read_jsonl(_results_path(ctx, session))):
        raise Refused(verb, "your verdict is recorded and took your runs into the evidence, so nothing after it "
                            "counts: hand back now with what you walked")  # fmt: skip
    return session, tester, str(records[-1]["head"])


def qa_finding(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester records one finding on the head it tests, as it finds it."""
    session, tester, full = _tester_on_head(ctx, args, "qa-finding")
    finding = findings.add(ctx, session.task, full, tester.role, ctx.caller.agent_id, args, data)
    return ok(finding=finding, head=full)


def qa_step(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester marks the start of one numbered step of an item, on the head it tests."""
    session, tester, full = _tester_on_head(ctx, args, "qa-step")
    return ok(**steps.start(ctx, session.id, full, tester.role, ctx.caller.agent_id, args))


def qa_step_result(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester records what it saw at the end of a step it started: pass, fail or blocked, in a few words."""
    session, tester, full = _tester_on_head(ctx, args, "qa-step-result")
    return ok(**steps.end(ctx, session.id, full, tester.role, ctx.caller.agent_id, args))


def qa_waive(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The orchestrator's ruling on a disputed finding: it no longer counts, for the reason given."""
    task, finding = args.get("task"), args.get("finding")
    if not isinstance(task, str) or not isinstance(finding, str):
        raise Refused("qa-waive", "factory qa waive <task> <finding> --why '…'")
    findings.waive(ctx, task, finding, str(args.get("why", "")))
    return ok(task=task, waived=finding)


def _walked_every_width(ctx: Context, session: Session, tester: Session, ready: dict[str, Any]) -> None:
    """Refused while a browser tester's walk never showed one of the widths its brief named, by its browser's own
    trace."""
    widths = lanes.checked_widths(ctx.settings(), ready, tester.started_as)
    if not widths:
        return
    missing = unwalked(ctx.places, qa_browser.runs_root(), session.id, tester.started_as, str(ready["at"]), widths)
    if missing:
        raise Refused(
            "qa-result",
            f"your browser's trace never shows the page at {', '.join(map(str, missing))} px wide, and a change to "
            f"the screens is walked at each of {', '.join(map(str, widths))} px. Resize to each with the resize tool, "
            "walk the changed screens there, then run factory qa result again. If something stopped you, say what: "
            "factory qa result --head <sha> --blocked '…'",
        )


def qa_result(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester's last step: its verdict on the head it tested, counted from the findings it recorded, and its runs
    into evidence. With nothing open it is refused until its walk is whole, unless it says what blocked it."""
    session, tester, full = _tester_on_head(ctx, args, "qa-result")
    blocked = args.get("blocked")
    if blocked is not None and (not isinstance(blocked, str) or not blocked.strip()):
        raise Refused("qa-result", "say what stopped you in a few words: --blocked '…'")
    records = ready_records(ctx, session)
    found = findings.counts(ctx, session.task, full, agent=ctx.caller.agent_id)
    counts = {"blockers": found["blocker"], "majors": found["major"], "minors": found["minor"]}
    failed = counts["blockers"] or counts["majors"]
    if not failed and blocked is None:
        _walked_every_width(ctx, session, tester, records[-1])
    said = {"blocked": one_line(blocked)} if blocked else {}
    result = {"head": full, "tester": tester.role, "agent": ctx.caller.agent_id, "at": ctx.clock.iso(), **counts}
    store.append_jsonl(_results_path(ctx, session), {**result, **said})
    verdict = f"{counts['blockers']} blockers, {counts['majors']} majors" if failed else "blocked" if said else "clean"
    doing.record(ctx.places, ctx.caller.agent_id, verdict=verdict)
    taken = evidence.intake(ctx, qa_browser.runs_root(), session.id, session.task, full, tester.started_as,
                            ctx.caller.agent_id, str(records[-1]["at"]))  # fmt: skip
    return ok(head=full, tester=tester.role, evidence=taken, **counts)


def qa_clean(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Refused until every required tester has a verdict on the final head with no open blocker or major, and none
    could not walk it. A clean verdict on an earlier head carries forward when the commits since it touch none of
    that tester's surfaces."""
    session = _implementer(ctx)
    records = ready_records(ctx, session)
    if not records or records[-1].get(REPRODUCE):
        raise Refused("qa-clean", "no ready record of your built slices: run factory qa-ready on your head first")
    head = str(records[-1]["head"])
    if _head(ctx, session) != head:
        raise Refused("qa-clean", f"your head moved past {head[:9]}: run qa-ready on it and test it again")
    task = tasks.read(ctx, session.task)
    latest: dict[str, dict[str, Any]] = {}  # each tester's newest verdict
    reported: set[str] = set()
    reproduced = {str(r["head"]) for r in records if r.get(REPRODUCE)}
    for result in store.read_jsonl(_results_path(ctx, session)):
        if result["head"] in reproduced:
            continue
        latest[str(result["tester"])] = result
        reported |= {str(result["agent"])} if result["head"] == head else set()
    # Two testers of one kind may test at once, each in its own browser lane: one's verdict is not the other's.
    walking = registry.testing(registry.read(ctx.places).values(), session.id)
    testing = sorted(s.name for s in walking if s.agent_id not in reported)
    missing: list[str] = []
    open_: list[str] = []
    blocked: list[str] = []
    for tester in records[-1].get("testers", []):
        tested = str(latest[tester]["head"]) if tester in latest else None
        if tested is None or (tested != head and tester in _touched(ctx, session, task, tested, head)):
            missing.append(tester)
        elif latest[tester].get("blocked"):
            blocked.append(f"{tester} could not walk it ({latest[tester]['blocked']})")
        elif findings.open_count(ctx, task.id, tested, tester):
            open_.append(tester)
    said = (
        ([f"no result from {', '.join(missing)} on what changed since it tested"] if missing else [])
        + ([f"{'; '.join(blocked)}: test again once that is fixed"] if blocked else [])
        + ([f"open blockers or majors from {', '.join(open_)}"] if open_ else [])
        + ([f"{', '.join(testing)} still testing: wait for its report"] if testing else [])
    )
    if said:
        raise Refused("qa-clean", f"not clean on {head[:9]}: {'; '.join(said)}")
    tasks.move(ctx, session.task, "qa-clean", "qa-clean", IMPL, f"QA clean at {head[:9]}")
    return ok(head=head, task=session.task)
