"""The guard's rules for subagents: which kinds a role may start (`subagent-kinds`), on which model (`model`), how
many at once (`subagents-at-once`), browser testers on the machine, none while a tester holds a desktop, the
orchestrator's first (`browsers-at-once`), a tester's brief (`tester-brief`), QA only after every slice
(`qa-ready`), QA and critic rounds (`qa-rounds`, `critic-rounds`), a tester held while the CPU is busy or an upgrade
gets ready and each tester's time (`cpu-hold`, `upgrade-hold`, `tester-time`, in walk_hold), and no finished subagent
resumed (`resume-subagent`).

Every count comes from the session's own transcript; the current call is left out by its ``tool_use_id``, and a
call the guard refused never ran.
"""

from __future__ import annotations

import re
from datetime import datetime

from runtime.guard import branches, factory_calls, shell, transcript, walk_hold
from runtime.guard.facts import (
    DESIGNER,
    IMPLEMENTER,
    ORCHESTRATOR,
    REPRODUCE,
    Call,
    Decision,
    Refusal,
    Rewrite,
    read_desktops,
    read_ready,
    read_vitals,
)
from runtime.guard.wire import HANDBACK, REFUSED
from runtime.shared import lanes
from runtime.shared.models import CRITIC, RETIRED, TESTER, WITH_PARENT
from runtime.shared.settings import model_role
from runtime.shared.settings_schema import SCHEMA
from runtime.warden.verbs import VERBS, slot_ops

INTAKE = "intake"
_SLOT_OP = re.compile(r"\bfactory slot ([a-z][a-z-]*)")
SLOT_API = "mcp__slot-api__slot_api"
# A session cannot see another's testers hand back: the warden tells it (runtime/warden/sessions/browser_waits.py).
WOKEN = "The factory messages you once a browser tester may start: do other work, or end your turn, until then"


def check_slot_session(call: Call) -> Decision:
    """A subagent's slot-api session carries its id, so no tester ends another's session of the same name."""
    name = call.tool_input.get("session")
    if not call.in_subagent or not isinstance(name, str) or not name:
        return None
    return Rewrite({**call.tool_input, "session": f"{name}.{call.agent_id}"})


def check_agent(call: Call) -> Decision:
    """A subagent start: its kind and model, the number running, a tester's brief, readiness and rounds; then its
    brief gains the factory's notes on calling factory and on when to reflect, as its kind may."""
    kind = lanes.kind_of(_started_as(call))
    current = str(call.payload.get("tool_use_id", ""))
    refused = transcript.refused_ids(call.transcript, REFUSED)
    found = transcript.agent_calls(call.transcript)
    ids = [c.tool_use_id for c in found]
    # Starts sent in one message are all in the transcript at the first one's hook: only those before this one count.
    earlier = found[: ids.index(current)] if current in ids else found
    calls = [c for c in earlier if c.tool_use_id not in refused]
    for rule in (_kind, _lane, _model, _browsers, _at_once, _brief, _qa, _critic_rounds, walk_hold.check):
        decision = rule(call, kind, calls)
        if decision is not None:
            return decision
    return _with_notes(call, kind)


def _kind(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    if call.off("subagent-kinds"):
        return None
    if call.in_subagent:
        return Refusal("subagent-kinds", "a subagent starts no agents of its own; report to your parent")
    key = f"subagents.{call.role}"
    allowed = call.settings.get_list(key) if key in SCHEMA else []
    name = _started_as(call)
    if kind not in allowed or name not in lanes.names(call.settings, kind):
        return Refusal("subagent-kinds", f"the {call.role} may start {', '.join(allowed) or 'no subagent'}, not {name}")
    if name != kind and call.session is not None and name not in call.session.kinds:
        return Refusal("subagent-kinds", f"this {call.role} has no {name}, a lane added since it started; wait for a "
                                         f"{kind} to report")  # fmt: skip
    return None


def _started_as(call: Call) -> str:
    return str(call.tool_input.get("subagent_type") or "general-purpose")


def _lane(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    """Two testers in one browser lane would share its browser, and the first to finish would close it on the other."""
    name = _started_as(call)
    if call.off("subagents-at-once") or kind not in call.settings.get_list("subagents.browser_personas"):
        return None
    taken = {c.subagent_type for c in calls if _running(call, c)}
    if name not in taken:
        return None
    has = call.session.kinds if call.session is not None else []
    free = [lane for lane in lanes.names(call.settings, kind) if lane not in taken and (lane == kind or lane in has)]
    then = f"start it as {free[0]}" if free else "wait for one to report"
    return Refusal("subagents-at-once", f"a {name} is running and its browser is that tester's; {then}")


def _model(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    asked = str(call.tool_input.get("model") or "")
    if not asked or call.off("model"):
        return None
    role = model_role(kind)
    configured = call.settings.model(role)[0] if role is not None else ""
    family = configured.split("-")[1] if configured.count("-") >= 1 else configured
    if configured and asked in {configured, family}:
        return None
    wanted = f"runs on {configured}" if configured else "has no model of its own to ask for"
    return Refusal("model", f"a {kind} {wanted}; leave the model out")


def _browsers(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    """At most caps.browsers_at_once browser testers on the machine, none on red vitals or while a tester holds a
    desktop: amber holds back new work. This session's earlier starts count too: one sent in the same message has no
    session row yet. A place the warden told a session of is kept for it for qa.place_turn_seconds, even while the
    orchestrator waits; no other goes to another while it waits: its merge check is what every merge waits on."""
    personas = call.settings.get_list("subagents.browser_personas")
    if kind not in personas or call.off(lanes.PLACES):
        return None
    waiting, told = lanes.line(call.places)
    me, kept = call.session.id if call.session else "", lanes.keeps(told, call.settings, call.now.timestamp())
    first = {s.id for s in call.sessions.values() if s.role == ORCHESTRATOR and not s.parent}
    if call.role != ORCHESTRATOR and me not in kept and first & set(waiting):
        return Refusal(lanes.PLACES, f"the orchestrator waits for a browser place for the merge check, which every "
                                     f"merge waits on, so it goes first. {WOKEN}")  # fmt: skip
    desktops = lanes.desktop_walks(call.sessions.values(), read_desktops(call.places), call.settings, call.now)
    if desktops:
        return Refusal(lanes.PLACES, f"a tester holds slot {min(desktops)}'s desktop, and a desktop walk runs alone: "
                                     "its live view needs the machine's spare CPU, so new browser walks wait until it "
                                     f"hands back. {WOKEN}")  # fmt: skip
    rows = lanes.holders(call.sessions.values(), call.settings, call.now)
    known = {s.agent_id for s in call.sessions.values() if s.agent_id}
    starting = [c for c in calls if lanes.kind_of(c.subagent_type) in personas and c.agent_id not in known
                and _running(call, c)]  # fmt: skip
    others = len(kept.keys() - {me})
    running, cap = len(rows) + len(starting), call.settings.get_int("caps.browsers_at_once")
    if running + others >= cap:
        also = f", and {others} kept for a session told one is free" if others else ""
        return Refusal(lanes.PLACES, f"{running} browser testers run on this machine{also}, its cap of {cap}. {WOKEN}")
    vitals = read_vitals(call.places)
    if vitals.level == "red":
        return Refusal(lanes.PLACES, f"the machine's vitals are red ({vitals.why}). {WOKEN}")
    return None


def _at_once(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    if call.off("subagents-at-once"):
        return None
    running = [c for c in calls if _running(call, c)]
    cap = call.settings.get_int("caps.subagents_at_once")
    # One place is kept for intake, so a severe request is never left waiting for a tester to finish.
    kept = call.role == ORCHESTRATOR and kind != INTAKE
    if len(running) < cap - kept:
        return None
    names = ", ".join(c.subagent_type for c in running)
    keep = "; one place is kept for intake" if kept else ""
    return Refusal("subagents-at-once", f"{len(running)} running ({names}){keep}; wait for one to report")


def _brief(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    if not kind.startswith(TESTER) or call.off("tester-brief"):
        return None
    try:
        transcript.declared_items(str(call.tool_input.get("prompt", "")), call.settings.get_int("qa.max_items"))
    # telemetry: surfaced — the `Refusal` names what the brief lacks
    except transcript.BriefError as exc:
        return Refusal("tester-brief", str(exc))
    unrun = sorted(set(_SLOT_OP.findall(str(call.tool_input.get("prompt", "")))) - slot_ops(kind))
    if unrun:
        return Refusal("tester-brief", f"a {kind} cannot run `factory slot {unrun[0]}`: do what the brief asks of the "
                                       "slot yourself before it starts, and give it what that showed")  # fmt: skip
    return None


def _running(call: Call, agent: transcript.AgentCall) -> bool:
    """Whether a started subagent still holds a place: not finished nor stopped, and not quiet past its budget or
    the stale limit."""
    stopped = {s.agent_id for s in call.sessions.values() if s.agent_id and s.state == RETIRED}
    if agent.finished or agent.agent_id in stopped:
        return False
    path = transcript.subagent_transcript(call.transcript, agent.agent_id) if agent.agent_id else None
    try:
        last = datetime.fromtimestamp(path.stat().st_mtime, tz=call.now.tzinfo) if path else agent.started
    # telemetry: expected — a subagent that has written nothing yet counts from its start
    except OSError:
        last = agent.started
    quiet = (call.now - last).total_seconds()
    if quiet >= call.settings.get_int("qa.stale_seconds"):
        return False
    budget = 0
    if agent.subagent_type.startswith(TESTER):
        try:
            items = transcript.declared_items(agent.prompt, call.settings.get_int("qa.max_items"))
            budget = lanes.tester_seconds(call.settings, items)
        # telemetry: expected — a tester started with no brief, while that rule was off, has no time budget
        except transcript.BriefError:
            budget = 0
    spent = (call.now - agent.started).total_seconds()
    return not (quiet >= call.settings.get_int("qa.quiet_seconds") and spent >= budget)


def _qa(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    """QA only after every slice: a ready record for this exact head, and at most the round limit of heads."""
    if not kind.startswith(TESTER) or call.role != IMPLEMENTER or call.off("qa-ready") or call.session is None:
        return None
    head = branches.head(call.worktree)
    records = read_ready(call.places, call.session.id)
    if head is None or not records or records[-1]["head"] != head:
        short = head[:9] if head else "your head"
        remedy = f"build every slice, then run `factory qa-ready {short} --slices …`"
        return Refusal("qa-ready", f"no ready record for {short}: {remedy}")
    if call.off("qa-rounds") or records[-1].get(REPRODUCE):
        return None
    # A round its subagents were interrupted in, when the session was benched, does not count, unless it is the
    # round this start continues.
    interrupted = _interrupted(call)
    heads: dict[str, bool] = {head: False}
    for agent in calls:
        if agent.subagent_type.startswith(TESTER):
            active = [r for r in records if datetime.fromisoformat(r["at"].replace("Z", "+00:00")) <= agent.started]
            if active and not active[-1].get(REPRODUCE):
                started_on = str(active[-1]["head"])
                heads[started_on] = heads.get(started_on, False) or agent.agent_id in interrupted
    rounds = sum(1 for h, cut in heads.items() if h == head or not cut)
    limit = call.settings.get_int("rounds.qa") + call.extension("qa")
    if rounds > limit:
        return Refusal("qa-rounds", f"this would be QA round {rounds} (limit {limit}); report to the orchestrator")
    return None


def _interrupted(call: Call) -> set[str | None]:
    """The subagents the warden retired with their parent when it benched the session."""
    rows = call.sessions.values()
    return {s.agent_id for s in rows if s.agent_id and s.state == RETIRED and s.retired == WITH_PARENT}


def _critic_rounds(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    """A critic round is a run of critic starts with no saved spec revision between them."""
    if not kind.startswith(CRITIC) or call.role != DESIGNER or call.off("critic-rounds"):
        return None
    refused = transcript.refused_ids(call.transcript, REFUSED)
    saves = [
        use.when
        for use in transcript.tool_uses(call.transcript, frozenset({"Bash"}))
        if use.tool_use_id not in refused and _saves_spec(str(use.args.get("command", "")))
    ]
    interrupted = _interrupted(call)
    starts = sorted(
        [(c.started, c.agent_id in interrupted) for c in calls if c.subagent_type.startswith(CRITIC)]
        + [(call.now, False)]
    )
    groups: list[bool] = []  # per round: whether a critic in it was interrupted by a bench
    previous = None
    for start, cut in starts:
        if previous is None or any(previous < save <= start for save in saves):
            groups.append(False)
        groups[-1] = groups[-1] or cut
        previous = start
    # A round interrupted by a bench does not count, unless it is the round this start continues.
    rounds = sum(1 for cut in groups[:-1] if not cut) + 1
    limit = call.settings.get_int("rounds.critic") + call.extension("critic")
    if rounds > limit:
        return Refusal("critic-rounds", f"this would be critic round {rounds} (limit {limit}); ask the orchestrator")
    return None


def _saves_spec(command: str) -> bool:
    try:
        found = shell.invocations(command)
    # telemetry: expected — a command the guard could not read was refused, so it saved nothing
    except shell.ShellError:
        return False
    return any(inv.program == factory_calls.FACTORY and factory_calls.verb_of(inv.argv) == "spec.save" for inv in found)


def _with_notes(call: Call, kind: str) -> Decision:
    """A brief gains the note on calling factory if its kind may, the note on reflecting if it may, and a tester's
    brief ends with the time the guard holds it to."""
    names = [name for name, may in (("factory-calls.md", any(kind in v.kinds for v in VERBS.values())),
                                    ("self-reflect.md", kind in VERBS["reflect"].kinds)) if may]  # fmt: skip
    prompt = str(call.tool_input.get("prompt", ""))
    texts = [(call.places.plugin / name).read_text(encoding="utf-8").strip() for name in names]
    if kind.startswith(TESTER) and not call.off("tester-brief"):
        items = transcript.declared_items(prompt, call.settings.get_int("qa.max_items"))
        texts.append(lanes.time_note(call.settings, items))
    notes = [n for n in texts if n not in prompt]
    if not notes:
        return None
    return Rewrite({**call.tool_input, "prompt": "\n\n".join([prompt.rstrip(), *notes]) + "\n"})


def check_send(call: Call) -> Decision:
    """A finished subagent, of any kind, is not resumed: a new check needs a new subagent, which counts."""
    target = str(call.tool_input.get("to", ""))
    if not target or call.off("resume-subagent"):
        return None
    stopped = {s.agent_id for s in call.sessions.values() if s.agent_id and s.state == RETIRED}
    finished = {a.agent_id for a in transcript.agent_calls(call.transcript) if a.finished and a.agent_id}
    if target in finished | stopped:
        return Refusal("resume-subagent", "a finished subagent is not resumed; start a new one (it counts)")
    return None


def check_intake_time(call: Call) -> Decision:
    """Intake's own calls once intake.fast_lane_minutes passed since its brief: only a plain `factory` call (it writes
    what it found with one) and its hand-back still go. A model cannot tell how long it has worked."""
    intake = call.in_subagent and call.agent_type == INTAKE
    if not intake or call.tool == HANDBACK or call.off("intake-time"):
        return None
    if call.tool == "Bash" and factory_calls.plain_words(str(call.tool_input.get("command", ""))) is not None:
        return None
    try:
        _, started = transcript.first_brief(transcript.subagent_transcript(call.transcript, call.agent_id))
    # telemetry: surfaced — the `Refusal` tells the intake subagent its time cannot be read
    except transcript.BriefError as exc:
        return Refusal("intake-time", f"your time cannot be read ({exc}); write what you found, then hand back")
    limit = call.settings.get_int("intake.fast_lane_minutes")
    if (call.now - started).total_seconds() < limit * 60:
        return None
    return Refusal("intake-time", f"your {limit} minutes are spent: write what you found with factory, then hand back")
