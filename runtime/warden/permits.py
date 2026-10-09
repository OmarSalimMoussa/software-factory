"""Who may start, and on what: the owner's approval (or the warden's own outage record), the vitals, the caps, the
model the approved rung names, and never the factory's own repo.

Every check raises Refused with what to do instead. The queue's tasks and statuses and the slots' leases belong to
their own modules; the calls into them sit at the bottom of this file, where a test replaces them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from runtime.queue import approvals, statuses, tasks
from runtime.queue.formats import FormatError
from runtime.shared import order, store
from runtime.shared.models import DEPRIORITIZED, RETIRED, WAITING, WORKING, Session, Task
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.warden import outage as outage_record
from runtime.warden import registry
from runtime.warden.context import Context

ORCH, IMPL, DESIGN = "orchestrator", "implementer", "designer"
FAMILY = {ORCH: "orch", IMPL: "impl", DESIGN: "design"}
CAP = {IMPL: "caps.implementers", DESIGN: "caps.designers"}
# The factory never works on itself. This is code, not a setting, so no saved setting can turn it off.
FACTORY_REPO = "software-factory"
LEVELS = ("green", "amber", "red", "unknown")
SEVERE = "severe"
_REMOTE_NAME = re.compile(r"([^/:]+?)(\.git)?/*$")


@dataclass(frozen=True, slots=True)
class Permit:
    role: str
    model: str
    effort: str
    rung: int
    bypass: bool  # started under the outage bypass, with no approval


def actor(ctx: Context) -> str:
    return "owner" if ctx.caller.kind in ("owner", "dashboard") else ctx.caller.role


def approved_order(ctx: Context) -> list[str]:
    raw = store.read_json_or(ctx.places.proposals, {"approved": []})
    approved = raw.get("approved") if isinstance(raw, dict) else None
    if not isinstance(approved, list):
        raise Refused("state", f"{ctx.places.proposals} holds no approved list; the warden's state needs a look")
    return [str(t) for t in approved]


def vitals_level(ctx: Context) -> str:
    try:
        raw = store.read_json(ctx.places.vitals)
    except FileNotFoundError:
        return "unknown"
    except store.StoreError as exc:
        raise Refused("vitals", f"the vitals cannot be read ({exc}); see factory status") from exc
    level = raw.get("level") if isinstance(raw, dict) else None
    if level not in LEVELS:
        raise Refused("vitals", "the vitals record names no level; see factory status")
    return str(level)


def check_vitals(ctx: Context, severe: bool) -> None:
    """Red blocks every start; amber, or vitals not read yet, block all but severe work."""
    level = vitals_level(ctx)
    if level == "red":
        raise Refused("vitals", "the machine's vitals are red, so nothing starts; see factory status")
    if level != "green" and not severe:
        raise Refused("vitals", f"the machine's vitals are {level}, so only severe work starts; see factory status")


def check_cap(settings: Settings, sessions: Iterable[Session], role: str) -> None:
    """Working and waiting sessions hold a place (and those between states); deprioritized and retired ones do not."""
    held = registry.seated(sessions, role)
    cap = settings.get_int(CAP[role])
    if len(held) >= cap:
        names = ", ".join(s.name for s in held)
        raise Refused("cap", f"{len(held)} of {cap} {role}s hold a place ({names}); bench one for higher work, or wait")


def cap_full(settings: Settings, sessions: Iterable[Session], role: str) -> bool:
    return len(registry.seated(sessions, role)) >= settings.get_int(CAP[role])


def check_revive_order(ctx: Context, sessions: dict[str, Session], role: str, task: Task, skip: str = "") -> None:
    """Deprioritized sessions come back before new runs they outrank, and among themselves in the one order."""
    approved = approved_order(ctx)
    for other in sessions.values():
        if other.id == skip or other.role != role or other.state != DEPRIORITIZED or other.parent:
            continue
        if order.outranks(read_task(ctx, other.task), task, approved):
            raise Refused("revive-first", f"revive {other.name} first: it outranks {task.id}")


def check_not_factory(ctx: Context) -> None:
    """Refuse when the product's repo, by its setting or by its checkout's remote, is the factory's own."""
    settings = ctx.settings()
    repo = settings.get_str("repo.path")
    code, out = ctx.launcher.agent_command(["git", "-C", repo, "remote", "get-url", "origin"], repo)
    if code != 0:
        raise Refused("repo", f"the repo's remote cannot be read: {out.strip()[-200:]}; check repo.path")
    for remote in (settings.get_str("repo.github"), out.strip()):
        found = _REMOTE_NAME.search(remote)
        if found and found.group(1).lower() == FACTORY_REPO:
            raise Refused("own-repo", "the factory never works on its own repo; point repo.path at a product")


def model_for(settings: Settings, role: str, rung: int, slices: int | None) -> tuple[str, str]:
    """The model and effort a rung's session runs: a designer, or the implementer for the rung or the spec's size."""
    if role == DESIGN:
        return settings.model("designer")
    if rung >= 3:
        if slices is None:
            raise Refused("rung", "the spec names no slice count, so the implementer's size is unknown; set slices")
        rung = 1 if slices <= settings.get_int("routing.small_max_slices") else 2
    return settings.model("implementer_small" if rung == 1 else "implementer_large")


def check_start(ctx: Context, sessions: dict[str, Session], role: str, task: Task, outage: bool) -> Permit:
    """The permit for a new implementer or designer on ``task``; raises Refused naming the first rule it breaks.

    Hold ctx.lock from here through the launch: an outage permit is recorded only once its session is started.
    """
    settings = ctx.settings()
    if role not in (IMPL, DESIGN):
        raise Refused("role", "a start is for an implementer or a designer")
    live = [s for s in sessions.values() if s.task == task.id and s.role == role and s.state != RETIRED]
    if live:
        raise Refused("one-run", f"{task.id} already has {live[0].name} ({live[0].state}); revive or stop it")
    if task.severity != SEVERE and not task.goal_item:
        raise Refused("goal", f"{task.id} names no goal item, and only severe work starts without one")
    if outage:
        if role != IMPL or task.run not in (1, 2):
            raise Refused("outage", "the outage bypass starts one implementer at rung 1 or 2, never a designer")
        if not outage_record.bypass_allowed(ctx):
            raise Refused("outage", "no outage bypass is open: prod is up or the fix already started; propose it")
        model, effort = model_for(settings, role, task.run, task.slices)
        return Permit(role, model, effort, task.run, bypass=True)
    approval = approvals.current(ctx.places, task.id)
    if approval is None:
        raise Refused("approval", f"the owner has not approved {task.id}; propose it")
    if not approvals.matches(approval, task):
        raise Refused("approval", f"{task.id} changed since the owner approved it; propose it again")
    rung = approval.rung
    designed = rung >= 3 and role == IMPL
    if role == DESIGN and rung < 3:
        raise Refused("rung", f"the owner approved rung {rung}, which needs no designer; start an implementer")
    want = "building" if designed else "approved"
    if task.status != want:
        raise Refused("status", f"{task.id} is {task.status}; this start needs it {want}")
    if designed and rung == 4 and not approvals.has_go(ctx.places, task.id):
        raise Refused("go", f"{task.id}'s spec waits for the owner's Go before its build starts")
    model, effort = model_for(settings, role, rung, task.slices)
    if not designed and approval.model != model:
        raise Refused(
            "approval", f"the owner approved {approval.model}, but rung {rung} runs {model} now; propose again"
        )
    return Permit(role, model, effort, rung, bypass=False)


def needed_role(ctx: Context, task_id: str) -> str:
    """The role a task's next session takes: a designer for an approved rung 3 or 4, else an implementer."""
    task = read_task(ctx, task_id)
    if task.status == "building":
        return IMPL
    approval = approvals.current(ctx.places, task.id)
    rung = approval.rung if approval is not None else task.run
    return DESIGN if rung is not None and rung >= 3 else IMPL


def benchable(ctx: Context, sessions: dict[str, Session], role: str, for_task: str) -> list[Session]:
    """The sessions a bench for ``for_task`` may push out, in the order to push them: waiting before working, then
    lowest-ranked first. Never one in its merge turn, and only one the new task outranks."""
    new, approved = read_task(ctx, for_task), approved_order(ctx)
    found = [(s, read_task(ctx, s.task)) for s in registry.seated(sessions.values(), role)
             if s.state in (WORKING, WAITING) and not s.merge_turn and s.task != for_task]  # fmt: skip
    found = [(s, t) for s, t in found if order.outranks(new, t, approved)]
    found.sort(key=lambda pair: order.rank(pair[1], approved), reverse=True)
    found.sort(key=lambda pair: pair[0].state != WAITING)  # stable: keeps the rank order within each state
    return [s for s, _ in found]


def in_order(ctx: Context, sessions: list[Session]) -> list[Session]:
    """The orchestrator first, then the rest in their tasks' one order."""
    approved = approved_order(ctx)
    ranked = sorted((s for s in sessions if s.task), key=lambda s: order.rank(read_task(ctx, s.task), approved))
    return [s for s in sessions if not s.task] + ranked


# ---- the queue's tasks and the slots' leases ----------------------------------------------------------------------


def read_task(ctx: Context, task_id: str) -> Task:
    try:
        return tasks.read(ctx, task_id)
    except FileNotFoundError as exc:
        raise Refused("task", f"no task {task_id}") from exc
    except (FormatError, UnicodeDecodeError) as exc:
        raise Refused("task", f"{task_id} cannot be read ({exc}); queue-format files it for a fix") from exc


def move_task(ctx: Context, task_id: str, to: str, event: str, by: str = statuses.WARDEN) -> None:
    tasks.move(ctx, task_id, to, event, by)


def claim_slot(ctx: Context, session: Session) -> int:
    return leases.claim(ctx, session.name, session.worktree, "revived")


def free_slot(ctx: Context, slot: int) -> None:
    leases.release(ctx, slot)
