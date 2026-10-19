"""The holds on new walks. A new tester waits while an upgrade gets ready (`upgrade-hold`), since the pause that
follows would cut its walk half way; the warden publishes until when, and the hold ends then even if the upgrade fails.

The CPU hold (`cpu-hold`): a host that meters CPU on credits holds the whole machine down once they run
out, and a walk's browsers and remote desktop run in containers outside the factory's slice, beyond the CPU limit the
warden sets on it: one walk costs about two CPUs. So while the machine's average CPU use is past
budget.walk_hold_percent, a new browser tester waits, and so does a session's own remote desktop; a tester's
desktop is part of the walk the hold let start, and is never held. An unknown reading holds them too, as
unknown is never fine: none published, one the warden could not take, or one older than budget.cpu_window_seconds,
the stretch its average covers.

No walk waits for good on a machine that stays busy: one held budget.walk_hold_minutes, counted from the first try the
hold refused since its requester last started one, starts anyway. The guard counts a session's browser testers from
its own transcript; the warden counts a slot's desktop itself (runtime/slots/verbs.py).
"""

from __future__ import annotations

import math
from datetime import datetime

from runtime.guard import transcript
from runtime.guard.facts import Call, Decision, Refusal, Vitals, read_vitals
from runtime.guard.wire import REFUSED, marker
from runtime.shared import lanes, store
from runtime.shared.models import TESTER
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

HOLD, UPGRADE = "cpu-hold", "upgrade-hold"


def why_held(vitals: Vitals, settings: Settings, now: datetime, since: datetime | None) -> str | None:
    """Why a new walk waits, naming the reading and the threshold; None once it may start. ``since`` is when the hold
    first refused it."""
    threshold = settings.get_float("budget.walk_hold_percent")
    age = None if vitals.at is None else (now - vitals.at).total_seconds()
    known = vitals.cpu if age is not None and age <= settings.get_int("budget.cpu_window_seconds") else None
    if known is not None and known <= threshold:
        return None
    longest = settings.get_int("budget.walk_hold_minutes")
    left = longest * 60 - (0 if since is None else (now - since).total_seconds())
    if left <= 0:
        return None
    anyway = f"or anyway in {math.ceil(left / 60)} min"
    if known is not None:
        return (
            f"the machine's CPU use averages {known:.0f}%, past the {threshold:.0f}% at which new walks wait "
            f"(budget.walk_hold_percent): this one starts once it falls, {anyway}"
        )
    return (
        f"the machine's CPU use is unknown ({_unknown(vitals, age)}), and new walks wait until it is known under "
        f"{threshold:.0f}% (budget.walk_hold_percent): this one starts once it is, {anyway}"
    )


def _unknown(vitals: Vitals, age: float | None) -> str:
    if age is None:
        return "the warden has published no reading"
    if vitals.cpu is None:
        return "the warden could not read it"
    return f"its last reading is {age / 60:.0f} min old"


def check(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    return _upgrade(call, kind) or _cpu(call, kind)


def _upgrade(call: Call, kind: str) -> Decision:
    until = _held_until(call.places)
    if not kind.startswith(TESTER) or call.off(UPGRADE) or until is None or until <= call.now:
        return None
    return Refusal(
        "upgrade-hold",
        f"the factory is about to upgrade, so new testers wait: do other work or end your turn, and start the {kind} "
        "once the factory is back",
    )


def _held_until(places: Places) -> datetime | None:
    raw = store.read_json_or(places.upgrade_hold, None)
    if raw is None:
        return None
    until = raw.get("until") if isinstance(raw, dict) else None
    if isinstance(until, str):
        try:
            return datetime.fromisoformat(until)
        # telemetry: surfaced — the `StoreError` below names the file, which the guard reports
        except ValueError:
            pass
    raise store.StoreError(f"{places.upgrade_hold}: holds no until time")


def _cpu(call: Call, kind: str) -> Decision:
    personas = call.settings.get_list("subagents.browser_personas")
    if kind not in personas or call.off(HOLD):
        return None
    why = why_held(read_vitals(call.places), call.settings, call.now, _held_since(call, personas))
    if why is None:
        return None
    return Refusal("cpu-hold", f"{why}; wait or do other work, then start the {kind} again")


def _held_since(call: Call, personas: list[str]) -> datetime | None:
    held, refused = (transcript.refused_ids(call.transcript, found) for found in (marker(HOLD), REFUSED))
    current, since = str(call.payload.get("tool_use_id", "")), None
    for agent in transcript.agent_calls(call.transcript):
        if agent.tool_use_id == current:
            break
        if lanes.kind_of(agent.subagent_type) not in personas:
            continue
        if agent.tool_use_id in held:
            since = since or agent.started
        elif agent.tool_use_id not in refused:
            since = None
    return since
