"""The holds on new walks, and each walk's time. A new tester waits while an upgrade gets ready (`upgrade-hold`), since
the pause that follows would cut its walk half way; the warden publishes until when, and the hold ends then even if the
upgrade fails.

The CPU hold (`cpu-hold`): a host that meters CPU on credits holds the whole machine down once they run
out, and a walk's browsers and remote desktop run in containers outside the factory's slice, beyond the CPU limit the
warden sets on it: one walk costs about two CPUs. So while the machine's average CPU use is past
budget.walk_hold_percent, a new browser tester waits, and so does a session's own remote desktop; a tester's
desktop is part of the walk the hold let start, and is never held. An unknown reading holds them too, as
unknown is never fine: none published, one the warden could not take, or one older than budget.cpu_window_seconds,
the stretch its average covers.

No walk waits for good on a machine that stays busy: one held budget.walk_hold_minutes, counted from the first try the
hold refused since its requester last started one, starts anyway. The guard counts a session's browser testers from
its own transcript; the warden counts a slot's desktop itself (runtime/slots/verbs.py). A walk lasts its tester's
time (`tester-time`), counted from its brief, then its report alone goes.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path

from runtime.guard import browser, factory_calls, scratch, transcript
from runtime.guard.facts import Call, Decision, Refusal, Rewrite, Vitals, read_desktops, read_vitals, resolve
from runtime.guard.wire import HANDBACK, REFUSED, marker
from runtime.shared import lanes, store
from runtime.shared.models import TESTER
from runtime.shared.paths import Places
from runtime.shared.settings import Settings
from runtime.warden.verbs import VERDICTS

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


# Claude Code's waits, in milliseconds: its defaults, and its longest, for a `factory` call the warden bounds itself.
_TIMED_TOOLS = {"Bash": ("timeout", 120_000)}
MONITOR = "a Monitor reports after you hand back: for a wait a step asks for, call `browser_wait_for` with `time`"
_LONGEST_BASH = 600_000
REPORT = (
    "record each step's result with `factory qa step-result` and what you found with `factory qa finding`, run "
    "`factory qa result`, then hand back"
)
_REPORTS = frozenset({"qa.step-result", "qa.finding", "qa.result", "reflect"})
# A report that says the tester's own time ran out, which only the guard can tell: one gave up a third of the way.
_OUT_OF_TIME = re.compile(
    r"\bout of time\b|\btime (?:ran|runs|has run|had run|is|was) (?:out|up)\b|\bno time left\b", re.I
)


def check_tester_time(call: Call) -> Decision:
    """A tester's own calls: refused once its time is spent, or its desktop time once it drives the remote desktop;
    shell and browser waits cut to the time left, but for `desktop up`, and a Monitor never. Its report always goes,
    uncut: a file in its scratch folder, its steps' results, findings and verdict, a reflection, and its hand-back."""
    tester = call.in_subagent and call.agent_type.startswith(TESTER)
    if not tester or call.tool == HANDBACK or call.off("tester-time"):
        return None
    if call.tool == "Monitor":
        return Refusal("tester-time", MONITOR)
    left, report = _time_left(call), reports(call)
    if isinstance(left, Refusal) and not report:
        return left
    if report and not isinstance(left, Refusal) and _OUT_OF_TIME.search(str(call.tool_input.get("command", ""))):
        said = "A tester cannot tell how long it has worked, so walk on until the factory refuses a call for time"
        return Refusal("tester-time", f"your time is not spent: {int(left)} s are left. {said}")
    timed = _TIMED_TOOLS.get(call.tool)
    if timed is None:
        return None if isinstance(left, Refusal) else browser.cap_wait(call, left)
    name, default = timed
    command = str(call.tool_input.get("command", "")) if call.tool == "Bash" else ""
    if factory_calls.plain_words(command) is not None:
        default = _LONGEST_BASH
    asked = call.tool_input.get(name)
    current = asked if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 else default
    # `desktop up` runs on the factory's time (`_time_left` leaves it out) and a report always goes: neither is cut.
    desktop = _starts_desktop(command)
    uncut = _LONGEST_BASH if desktop else current
    most = uncut if report or desktop or isinstance(left, Refusal) else min(current, int(left * 1000))
    updated = {**call.tool_input, name: max(1_000, most)}
    if call.tool == "Bash":
        updated["run_in_background"] = False
    return Rewrite(updated) if updated != call.tool_input else None


def reports(call: Call) -> bool:
    """A tester's report: a file in its scratch folder, a step's result, a finding, its verdict or a reflection."""
    if call.tool == "Write":
        return scratch.in_scratch(call, resolve(str(call.tool_input.get("file_path", "")), call.cwd, call.home))
    words = factory_calls.plain_words(str(call.tool_input.get("command", ""))) if call.tool == "Bash" else None
    return words is not None and factory_calls.verb_of(words) in _REPORTS


def _time_left(call: Call) -> float | Refusal:
    """Seconds left of the tester's budget: its items' time, or its desktop time once it drives the remote desktop."""
    path = transcript.subagent_transcript(call.transcript, call.agent_id)
    try:
        brief, started = transcript.first_brief(path)
        items = transcript.declared_items(brief, call.settings.get_int("qa.max_items"))
    # telemetry: surfaced — `_time_up` tells the tester to report what it has
    except transcript.BriefError as exc:
        return _time_up(call, f"your time budget cannot be read ({exc})")
    budget = float(lanes.tester_seconds(call.settings, items))
    first_up, in_ups = _desktop_starts(call, path)
    spent = (call.now - started).total_seconds() - in_ups
    # On a desktop a tester works to the desktop's end, its items' time aside; that counts from its first `desktop up`.
    if call.agent_id in read_desktops(call.places).values():
        desktop_s = call.settings.get_int("qa.desktop_seconds")
        on_desktop = (call.now - (first_up or started)).total_seconds() - in_ups
        if on_desktop >= desktop_s:
            return _time_up(call, f"the desktop is a tester's for {desktop_s} s, and goes down once you hand back")
        return max(budget - spent, desktop_s - on_desktop)
    return budget - spent if budget > spent else _time_up(call, "your time is up")


def _desktop_starts(call: Call, path: Path) -> tuple[datetime | None, float]:
    """The tester's first `factory slot desktop up`, and the seconds in them all (the factory's, not the tester's)."""
    ups = [use for use in transcript.tool_uses(path, frozenset({"Bash"})) if _starts_desktop(use.args.get("command"))]
    done = transcript.answered(path) if ups else {}
    return ups[0].when if ups else None, sum((done.get(u.tool_use_id, call.now) - u.when).total_seconds() for u in ups)


def _starts_desktop(command: object) -> bool:
    words = factory_calls.plain_words(command) if isinstance(command, str) else None
    return words is not None and words[1:4] == ["slot", "desktop", "up"]


def _time_up(call: Call, why: str) -> Refusal:
    return Refusal("tester-time", f"{why}: {report_now(call)}")


def report_now(call: Call) -> str:
    """A tester that records a verdict reports through `factory`; the others hand their report back."""
    return REPORT if call.agent_type in VERDICTS else "hand back your report now"
