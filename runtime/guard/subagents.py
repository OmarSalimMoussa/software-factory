"""The guard's rules for subagents: which kinds a role may start (`subagent-kinds`), on which model (`model`), how
many at once (`subagents-at-once`), a tester's brief (`tester-brief`), QA only after every slice (`qa-ready`), QA and
critic rounds (`qa-rounds`, `critic-rounds`), each tester's time (`tester-time`), and no finished subagent resumed
(`resume-subagent`).

Every count comes from the session's own transcript; the current call is left out by its ``tool_use_id``, and a
call the guard refused never ran.
"""

from __future__ import annotations

from datetime import datetime

from runtime.guard import branches, browser, factory_calls, shell, transcript
from runtime.guard.facts import (
    DESIGNER,
    IMPLEMENTER,
    ORCHESTRATOR,
    Call,
    Decision,
    Refusal,
    Rewrite,
    read_ready,
)
from runtime.guard.wire import REFUSED
from runtime.shared.models import RETIRED, WITH_PARENT
from runtime.shared.settings import SCHEMA

INTAKE = "intake"
TESTER, CRITIC = "qa-", "critic-"
# Claude Code's own timeouts when a call names none, in milliseconds.
_TIMED_TOOLS = {"Bash": ("timeout", 120_000), "Monitor": ("timeout_ms", 300_000)}
TIME_UP = "your time is up: report what you have"


def check_agent(call: Call) -> Decision:
    """A subagent start: its kind and model, the number running, a tester's brief, readiness and rounds; then its
    brief gains the factory's note on when to reflect."""
    kind = str(call.tool_input.get("subagent_type") or "general-purpose")
    current = str(call.payload.get("tool_use_id", ""))
    refused = transcript.refused_ids(call.transcript, REFUSED)
    calls = [c for c in transcript.agent_calls(call.transcript) if c.tool_use_id not in refused | {current}]
    for rule in (_kind, _model, _at_once, _brief, _qa, _critic_rounds):
        decision = rule(call, kind, calls)
        if decision is not None:
            return decision
    return _with_reflection(call)


def _kind(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    if call.off("subagent-kinds"):
        return None
    if call.in_subagent:
        return Refusal("subagent-kinds", "a subagent starts no agents of its own; report to your parent")
    key = f"subagents.{call.role}"
    allowed = call.settings.get_list(key) if key in SCHEMA else []
    if kind not in allowed:
        return Refusal("subagent-kinds", f"the {call.role} may start {', '.join(allowed) or 'no subagent'}, not {kind}")
    return None


def model_key(kind: str) -> str:
    """The setting that holds a subagent kind's model: every critic shares one."""
    return "models.critic" if kind.startswith(CRITIC) else "models." + kind.replace("-", "_")


def _model(call: Call, kind: str, calls: list[transcript.AgentCall]) -> Decision:
    asked = str(call.tool_input.get("model") or "")
    if not asked or call.off("model"):
        return None
    key = model_key(kind)
    configured = str(call.settings[key]["model"]) if key in SCHEMA else ""
    family = configured.split("-")[1] if configured.count("-") >= 1 else configured
    if configured and asked in {configured, family}:
        return None
    wanted = f"runs on {configured}" if configured else "has no model of its own to ask for"
    return Refusal("model", f"a {kind} {wanted}; leave the model out")


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
    # The refusal names what the brief lacks.
    except transcript.BriefError as exc:
        return Refusal("tester-brief", str(exc))
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
    # A subagent that has written nothing yet counts from its start.
    except OSError:
        last = agent.started
    quiet = (call.now - last).total_seconds()
    if quiet >= call.settings.get_int("qa.stale_seconds"):
        return False
    budget = 0
    if agent.subagent_type.startswith(TESTER):
        try:
            items = transcript.declared_items(agent.prompt, call.settings.get_int("qa.max_items"))
            budget = items * call.settings.get_int("qa.seconds_per_item")
        # A tester started with no brief, while that rule was off, has no time budget.
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
    if call.off("qa-rounds"):
        return None
    # A round its subagents were interrupted in, when the session was benched, does not count, unless it is the
    # round this start continues.
    interrupted = _interrupted(call)
    heads: dict[str, bool] = {head: False}
    for agent in calls:
        if agent.subagent_type.startswith(TESTER):
            active = [r for r in records if _at(r["at"]) <= agent.started]
            if active:
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


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


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
    # A command the guard could not read was refused, so it saved nothing.
    except shell.ShellError:
        return False
    return any(inv.program == factory_calls.FACTORY and factory_calls.verb_of(inv.argv) == "spec.save" for inv in found)


def _with_reflection(call: Call) -> Decision:
    """Every subagent's brief ends with the factory's note on when to write to SELF_REFLECT.md."""
    note = (call.places.plugin / "self-reflect.md").read_text(encoding="utf-8").strip()
    prompt = str(call.tool_input.get("prompt", ""))
    if note in prompt:
        return None
    return Rewrite({**call.tool_input, "prompt": f"{prompt.rstrip()}\n\n{note}\n"})


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


def check_tester_time(call: Call) -> Decision:
    """A tester's own calls: refused once its time per scenario is spent, or its desktop time once it drives the
    remote desktop; shell and browser waits cut to the time left. Its hand-back always goes: that is how it reports."""
    tester = call.in_subagent and call.agent_type.startswith(TESTER)
    if not tester or call.tool == "SubagentHandback" or call.off("tester-time"):
        return None
    left = _time_left(call)
    if isinstance(left, Refusal):
        return left
    timed = _TIMED_TOOLS.get(call.tool)
    if timed is None:
        return browser.cap_wait(call, left)
    name, default = timed
    asked = call.tool_input.get(name)
    current = asked if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 else default
    updated = {**call.tool_input, name: max(1_000, min(current, int(left * 1000)))}
    if call.tool == "Bash":
        updated["run_in_background"] = False
    return Rewrite(updated) if updated != call.tool_input else None


def check_intake_time(call: Call) -> Decision:
    """Intake's own calls once intake.fast_lane_minutes passed since its brief: only a plain `factory` call (it writes
    what it found with one) and its hand-back still go. A model cannot tell how long it has worked."""
    intake = call.in_subagent and call.agent_type == INTAKE
    if not intake or call.tool == "SubagentHandback" or call.off("intake-time"):
        return None
    if call.tool == "Bash" and factory_calls.plain_words(str(call.tool_input.get("command", ""))) is not None:
        return None
    try:
        _, started = transcript.first_brief(transcript.subagent_transcript(call.transcript, call.agent_id))
    except transcript.BriefError as exc:
        return Refusal("intake-time", f"your time cannot be read ({exc}); write what you found, then hand back")
    limit = call.settings.get_int("intake.fast_lane_minutes")
    if (call.now - started).total_seconds() < limit * 60:
        return None
    return Refusal("intake-time", f"your {limit} minutes are spent: write what you found with factory, then hand back")


def _time_left(call: Call) -> float | Refusal:
    """Seconds left of the tester's budget: its items' time, and at most the desktop time once it drives the
    remote desktop."""
    path = transcript.subagent_transcript(call.transcript, call.agent_id)
    try:
        brief, started = transcript.first_brief(path)
        items = transcript.declared_items(brief, call.settings.get_int("qa.max_items"))
    # The refusal tells the tester to report what it has.
    except transcript.BriefError as exc:
        return Refusal("tester-time", f"your time budget cannot be read ({exc}); report what you have")
    budget = float(items * call.settings.get_int("qa.seconds_per_item"))
    desktop_s = call.settings.get_int("qa.desktop_seconds")
    spent = (call.now - started).total_seconds()
    desktop = call.tool.startswith(browser.DESKTOP)
    if desktop and spent >= desktop_s:
        return Refusal("tester-time", f"the remote desktop is for a tester's first {desktop_s} s; report what you have")
    if desktop or transcript.used_any(path, browser.DESKTOP):
        budget = min(budget, desktop_s)
    return budget - spent if budget > spent else Refusal("tester-time", TIME_UP)
