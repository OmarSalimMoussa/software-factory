"""`factory slot …`: the warden runs the product's scripts/dev for the caller's own slot (factory spec, "Slots").

The slot is always worked out from the caller's lease, never from an argument: slot 0 is the orchestrator's, an
implementer has the slot it claimed, and its testers share it. A stack command before ``claim`` is refused. The
answer carries scripts/dev's exit status and the tail of its output; the full log stays the owner's.

A tester only reads its slot and what runs on it, signs in and opens its desktop: seeds and the stack are its
parent's. The slot's own session, an implementer or the orchestrator, may run one command in a new container of a
service, such as a scenario's set-up; so may its backend and security testers, to look from inside the slot's
network. One tester holds a slot's desktop at a time, until its parent or, once it hands back, the warden takes it
down; another tester's `desktop up` waits, so no one's new password cuts off a tester mid-step. While the machine's
CPU use is past budget.walk_hold_percent, a `desktop up` waits too (runtime/guard/walk_hold.py); the wait counts per
slot, in the warden's memory, so a restart begins it again. Slot 0 is staging, so it comes up whole: one build of the
stack, never a service at a time. No stack changes under a tester still walking it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from runtime.guard import walk_hold
from runtime.guard.facts import read_desktops, read_vitals
from runtime.shared import store
from runtime.shared.models import RETIRED, Session
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.warden import public
from runtime.warden.context import Context, StackResult
from runtime.warden.sessions import registry
from runtime.warden.verbs import BACKEND, IMPL, ORCH, SECURITY

WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
EMAIL = re.compile(r"^[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
# Verbs a tester may run on its implementer's slot; the rest change the stack and are the implementer's own.
TESTER_OPS = frozenset({"list", "which", "sign-in", "desktop", "top"})
RUNNER_OPS = TESTER_OPS | {leases.RUN}
_CHANGES_STACK = frozenset({"up", "down", leases.RESTART})
_FIXED = {"down": ["down"], "top": [leases.TOP]}
# By state folder and slot: when the CPU hold first refused that slot's desktop since it last went up.
_HELD: dict[tuple[str, int], datetime] = {}


def _session(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.state == RETIRED:
        raise Refused("slot", "only a live factory session has a slot")
    return session


def _slot(ctx: Context, session: Session) -> int:
    if session.role == ORCH:
        return 0
    lease = leases.held_by(ctx.places, ctx.settings(), session.name)
    if lease is None:
        raise Refused("slot", "claim a slot first: factory slot claim")
    return lease.slot


def _words(values: object, what: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(v, str) and WORD.match(v) for v in values):
        raise Refused("args", f"{what} are plain words, none starting with a dash")
    return list(values)


def _email(value: object) -> str:
    if not isinstance(value, str) or not EMAIL.match(value):
        raise Refused("args", "name a test address such as tester-1@example.com")
    return value


def _answer(ctx: Context, n: int, result: StackResult) -> dict[str, Any]:
    """The verb's exit and output; a failure soon after the laptop woke says so, since its network may have lagged."""
    woke = store.read_json_or(ctx.places.woke, None) if result.exit != 0 else None
    minutes = int(ctx.clock.age(woke["at"]) // 60) if woke is not None else None
    if minutes is None or minutes >= ctx.settings().get_int("slots.wake_note_minutes"):
        return ok(slot=n, exit=result.exit, tail=result.tail)
    note = f"this machine woke from sleep {minutes} min ago: if this failed reaching the network, try it again"
    return ok(slot=n, exit=result.exit, tail=result.tail, note=note)


def slot(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    op = args.get("op")
    session = _session(ctx)
    allowed = RUNNER_OPS if ctx.caller.agent_kind in (BACKEND, SECURITY) else TESTER_OPS
    if ctx.caller.agent_id and op not in allowed:
        raise Refused("slot", f"a {ctx.caller.agent_kind or 'tester'} may run {', '.join(sorted(allowed))} on its "
                              f"slot, not {op}")  # fmt: skip
    settings = ctx.settings()
    if op == "claim":
        if session.role != IMPL:
            raise Refused("slot", "only an implementer claims a slot; slot 0 is the orchestrator's")
        n = leases.claim(ctx, session.name, str(args.get("note", "")))
        return ok(slot=n, port=leases.port(settings, n))
    if op == "list":
        return ok(slots=_list(ctx, session))
    if op == "free":
        return _free(ctx, session, args.get("slot"))
    n = _slot(ctx, session)
    if op == "which":
        return ok(
            slot=n,
            port=leases.port(settings, n),
            timeout_seconds=settings.get_int("slots.api_timeout_seconds"),
            max_kib=settings.get_int("slots.api_max_kib"),
        )
    if op == "release":
        failed = leases.empty(ctx, n, session.id)
        return ok(slot=n, released=not failed, **({"failed": failed} if failed else {}))
    verb = _verb(op, args)
    if op in _CHANGES_STACK:
        _may_change(ctx, session, n, verb)
    if op == "desktop":
        _desktop_lease(ctx, n, verb)
    result = ctx.launcher.run_stack(n, verb, session.id)
    return _answer(ctx, n, result)


def _may_change(ctx: Context, session: Session, n: int, verb: list[str]) -> None:
    if n == 0 and verb[0] == "up" and len(verb) > 1:
        raise Refused("slot", "slot 0 is staging, one build of the whole stack: factory slot up, naming no service")
    walking = registry.testing(registry.read(ctx.places).values(), session.id)
    if walking:
        names = ", ".join(sorted(s.name for s in walking))
        raise Refused("slot", f"{names} still testing slot {n}: change its stack once they report")


def _desktop_lease(ctx: Context, n: int, verb: list[str]) -> None:
    """A tester's `desktop up` takes the slot's desktop while no other tester holds it, within caps.desktops_at_once."""
    agent = ctx.caller.agent_id
    if agent and verb[1] != "up":
        raise Refused("slot", "a tester opens the desktop; its implementer takes it down")
    with ctx.lock:
        held = read_desktops(ctx.places)
        if agent:
            _may_take(ctx, n, agent, held)
        # A tester that holds the desktop already walks it: taking it up again starts no new walk.
        if verb[1] == "up" and held.get(str(n)) != agent:
            _cpu_hold(ctx, n, bool(agent))
        if agent:
            held[str(n)] = agent
        else:
            held.pop(str(n), None)
        store.write_json(public.desktops_path(ctx.places), held)


def _may_take(ctx: Context, n: int, agent: str, held: dict[str, str]) -> None:
    sessions = registry.read(ctx.places)
    holder = sessions.get(held.get(str(n), ""))
    if holder is not None and holder.id != agent:
        if holder.state == RETIRED:
            raise Refused("desktop", f"slot {n}'s desktop is going down after {holder.name} handed back; "
                                     "try again in a minute")  # fmt: skip
        raise Refused("desktop", f"slot {n}'s desktop is {holder.name}'s until it hands back; test what "
                                 "needs no desktop first, then try again")  # fmt: skip
    up = [s for slot, s in held.items() if slot != str(n) and s in sessions and sessions[s].state != RETIRED]
    cap = ctx.settings().get_int("caps.desktops_at_once")
    if len(up) >= cap:
        raise Refused("desktop", f"{len(up)} desktops are up on this machine, its cap of {cap}; test what "
                                 "needs no desktop first, then try again")  # fmt: skip


def _cpu_hold(ctx: Context, n: int, tester: bool) -> None:
    key = (str(ctx.places.state), n)
    now = datetime.fromtimestamp(ctx.clock.now(), UTC)
    off = walk_hold.HOLD in public.read_switches(ctx.places)
    why = None if off else walk_hold.why_held(read_vitals(ctx.places), ctx.settings(), now, _HELD.get(key))
    if why is None:
        _HELD.pop(key, None)
        return
    _HELD.setdefault(key, now)
    then = "test what needs no desktop first, then try again" if tester else "wait or do other work, then try again"
    raise Refused(walk_hold.HOLD, f"{why}; {then}")


def _verb(op: object, args: dict[str, Any]) -> list[str]:
    """The scripts/dev command line for one slot operation, from checked words only."""
    if op in ("up", leases.RESTART):
        return _services_verb(str(op), args)
    if isinstance(op, str) and op in _FIXED:
        return list(_FIXED[op])
    if op == "sign-in":
        return ["sign-in", _email(args.get("email"))]
    if op == "seed":
        return _seed_verb(args)
    if op == "desktop":
        return _desktop_verb(args)
    if op == leases.RUN:
        return _run_verb(args)
    raise Refused(
        "args",
        f"{op!r} is not a slot verb: claim, up, down, restart <service…>, release, free, list, sign-in, seed, "
        "desktop, run, top",
    )


def _services_verb(op: str, args: dict[str, Any]) -> list[str]:
    services = _words(args.get("services", []), "services")
    if op == leases.RESTART and not services:
        raise Refused("args", "restart <service…>: name each service to restart")
    return [op, *services]


def _seed_verb(args: dict[str, Any]) -> list[str]:
    words = _words(args.get("seed", []), "seed words")
    if words[:1] == ["save"] and len(words) == 3 or words == ["check"]:
        return ["seed", *words]
    if words[:1] == ["load"] and len(words) == 2:
        return ["seed", *words, *(["--as", _email(args["as"])] if args.get("as") else [])]
    raise Refused("args", "seed save <id> <name> | seed load <name> [--as <email>] | seed check")


def _desktop_verb(args: dict[str, Any]) -> list[str]:
    words = _words(args.get("desktop", []), "desktop words")
    if words == ["down"]:
        return ["desktop", "down"]
    if words[:1] == ["up"] and len(words) == 2:
        return ["desktop", "up", _email(words[1])]
    raise Refused("args", "desktop up <email> | desktop down")


def _run_verb(args: dict[str, Any]) -> list[str]:
    """The command is handed on as its own words, never through a shell, so any word may follow the service."""
    service, command = args.get("service"), args.get("command")
    if not isinstance(command, list) or not all(isinstance(word, str) and "\0" not in word for word in command):
        command = []
    if not isinstance(service, str) or not WORD.match(service) or not command:
        raise Refused("args", "run <service> -- <command> [word…]")
    return [leases.RUN, service, "--", *command]


def _list(ctx: Context, session: Session) -> list[dict[str, Any]]:
    settings = ctx.settings()
    rows: list[dict[str, Any]] = [
        {"slot": 0, "port": leases.port(settings, 0), "holder": "the main checkout", "mine": session.role == ORCH}
    ]
    for n in leases.numbers(ctx.places, settings):
        lease = leases.read(ctx.places, n)
        rows.append(
            {
                "slot": n,
                "port": leases.port(settings, n),
                "holder": lease.holder if lease else "",
                "since": lease.since if lease else "",
                "note": lease.note if lease else "",
                "mine": bool(lease) and lease is not None and lease.holder == session.name,
            }
        )
    return rows


def _free(ctx: Context, session: Session, n: object) -> dict[str, Any]:
    """The orchestrator empties and frees a finished session's slot; a live holder keeps it."""
    if session.role != ORCH:
        raise Refused("slot", "slot free is the orchestrator's, for finished work")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise Refused("args", "name the slot to free: factory slot free <n>")
    lease = leases.read(ctx.places, n)
    if lease is None:
        raise Refused("slot", f"slot {n} is already free")
    holder = registry.by_name(registry.read(ctx.places).values(), lease.holder)
    if holder is not None:
        raise Refused("slot", f"slot {n} is held by {lease.holder}, which is {holder.state}; it releases its own slot")
    failed = leases.empty(ctx, n, session.id)
    return ok(slot=n, released=not failed, **({"failed": failed} if failed else {}))
