"""The drill that an agent revived with today's agent definitions keeps its conversation.

The warden revives an agent whose definitions are not today's as a copy: `claude --bg --resume <id>` with today's
options, which Claude Code starts under a new id with the stopped session's whole conversation. That is Claude Code's
own behaviour, which no version promises to keep, so each install proves it: the copy runs under a new id,
remembers a word said to the original, and has an agent definition the original lacked.
"""

from __future__ import annotations

import json
import uuid

from runtime.system.install_drills.evidence import POLL_S, Ran, Result, Rig, first_line, recorded
from runtime.warden.sessions import session_settings
from runtime.warden.sessions.launch import BACKGROUNDED

WORD, PROBE = "pelican", "drill-probe"
REMEMBER = f"This is a drill. Remember the word {WORD}. Reply with the one word ok, and do nothing else."
RECALL = (f"This is a drill. Reply with the word you were asked to remember, then yes if one of your agent types is "
          f"named {PROBE} or no if none is, and do nothing else.")  # fmt: skip


def copy_remembers(rig: Rig) -> Result:
    name = "an agent revived with new agent definitions comes back as a copy that remembers"
    tag = f"drill-renew-{uuid.uuid4().hex[:8]}"
    deadline = rig.now() + rig.settings.get_int("install.drill_seconds")
    first = _answered(rig, rig.real_session(tag, REMEMBER), "ok", deadline)
    if isinstance(first, Result):
        return Result(name, False, f"the first session: {first.evidence}")
    sid, short, said = first
    rig.stop_session(short)
    if "ok" not in said.lower():
        return Result(name, False, f"the first session never answered as asked: {first_line(said) or 'nothing'}")
    while sid in rig.session_records() and rig.now() <= deadline:
        rig.wait(POLL_S)
    model = rig.settings["install.drill"]
    today = session_settings.agents(rig.settings, rig.places, "implementer", model["model"], model["effort"])
    probe = {PROBE: {"description": "A drill's probe: never start it.", "prompt": "Reply with the one word ok."}}
    resumed = rig.real_session(tag, RECALL, resume=sid, agents=json.dumps(today | probe))
    copy = _answered(rig, resumed, WORD, deadline)
    if isinstance(copy, Result):
        return Result(name, False, f"the copy: {copy.evidence}")
    copy_sid, copy_short, said = copy
    rig.stop_session(copy_short)
    if copy_sid == sid:
        return Result(name, False, f"it came back under its own id, {short}, with the definitions it started with")
    if WORD not in said.lower() or "yes" not in said.lower():
        return Result(name, False, f"the copy did not remember, or lacks {PROBE}: {first_line(said)!r}")
    return Result(name, True, f"{short} came back as {copy_short}, which said {first_line(said)!r}")


def _answered(rig: Rig, ran: Ran, wanted: str, deadline: float) -> tuple[str, str, str] | Result:
    """The session a start named, its short id, and its answer once it says ``wanted``; a failed Result if none ran."""
    started = BACKGROUNDED.search(ran.out)
    if started is None:
        return Result("", False, f"claude --bg printed no 'backgrounded · <id>' line: {first_line(ran.out)}")
    short = started.group(1)
    found = recorded(rig, short, deadline)
    if found is None:
        return Result("", False, f"no record names session {short}")
    sid = found[0]
    while wanted not in (said := rig.session_said(sid)).lower() and rig.now() <= deadline:
        rig.wait(POLL_S)
    return sid, short, said
