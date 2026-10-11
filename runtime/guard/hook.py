"""The Claude Code hook for every factory session: PreToolUse, SessionStart, Stop, SubagentStart and SubagentStop.

Run as ``/usr/bin/python3 -I <install>/runtime/guard/hook.py``. Before loading any rule it decides from the kernel
alone whether this is a factory session: its process sits in the factory's slice. A session outside the slice is the
owner's and is never guarded. Inside it, any failure refuses the call with exit 2, because Claude Code lets a call
through on a timeout or any other exit code; reading and reporting are never refused for a failure. Each refusal is
one line naming the rule and the remedy, and goes to the warden's refusal socket. The guard decides within half the
hook's time (runtime/guard/deadline.py), so a slow rule refuses instead of timing out. A subagent's start and end are
reported to the warden, which registers it under its parent; those hooks never block. Stdlib only.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.guard import deadline, wire  # noqa: E402 - the path line above must run first
from runtime.shared.paths import SLICE, Places  # noqa: E402

_INPUT_LIMIT = 4_000_000
_DETAIL_LIMIT = 2000
Outcome = tuple[int, str, str]  # exit code, standard output, standard error


def in_factory_slice(cgroup_text: str) -> bool:
    return any(f"/{SLICE}/" in line + "/" for line in cgroup_text.splitlines())


def _refuse(places: Places, payload: dict[str, object], rule: str, reason: str, now: datetime) -> Outcome:
    record = {
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": str(payload.get("session_id", "")),
        "rule": rule,
        "tool": str(payload.get("tool_name") or payload.get("hook_event_name") or ""),
        "detail": " ".join(reason.split())[:_DETAIL_LIMIT],
    }
    wire.send_refusal(places.refusal_socket, record)
    return 2, "", wire.one_line(rule, reason)


def _context(text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def handle(raw: str, cgroup: str, places: Places, home: Path, now: datetime) -> Outcome:
    if not in_factory_slice(cgroup):
        return 0, "", ""
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("the hook input is not an object")
    except ValueError:
        return _refuse(places, {}, "guard", "the hook input could not be read; report this to the owner", now)
    event = payload.get("hook_event_name")
    if event in _SUBAGENT_VERBS:
        return 0, "", report_subagent(payload, str(event), places)
    try:
        return _run(payload, event, places, home, now)
    # Any failure, a broken rule module included, refuses a factory call; the reason names it.
    except Exception as exc:  # noqa: BLE001
        failure = f"the guard failed ({type(exc).__name__}: {exc})"
        if event == "SessionStart":
            return 0, _context(f"{failure}; tell the owner before you do anything."), ""
        if event != "PreToolUse" or wire.simple_way_out(payload):
            return 0, "", ""
        return _refuse(places, payload, "guard", f"{failure}, so this call is refused; report it to the owner", now)


_SUBAGENT_VERBS = {"SubagentStart": "subagent.start", "SubagentStop": "subagent.end"}


def report_subagent(payload: dict[str, object], event: str, places: Places) -> str:
    """Tell the warden a subagent started or ended; what went wrong, as one line for standard error, or nothing."""
    verb = _SUBAGENT_VERBS[event]
    try:
        from runtime.shared.protocol import request

        args = {"agent_id": str(payload.get("agent_id", ""))}
        if verb == "subagent.start":
            args["kind"] = str(payload.get("agent_type", ""))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(deadline.left())
            sock.connect(str(places.socket))
            sock.sendall(request(verb, args))
            answer = json.loads(sock.makefile("rb").readline())
    # Never raised: the subagent runs either way, and its factory calls are refused until the warden knows it.
    except Exception as exc:  # noqa: BLE001
        return f"factory guard: {verb} was not sent ({type(exc).__name__})"
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        rule = answer.get("refused", "?") if isinstance(answer, dict) else "?"
        return f"factory guard: the warden refused {verb} ({rule})"
    return ""


def _run(payload: dict[str, object], event: object, places: Places, home: Path, now: datetime) -> Outcome:
    from runtime.guard import decide
    from runtime.guard.facts import Refusal, Rewrite

    call = decide.resolve(payload, places, now, home)
    if call is None:
        return 0, "", ""
    if event == "SessionStart":
        return 0, _context(decide.session_context(call)), ""
    decision = None
    if event == "Stop":
        decision = decide.check_stop(call)
    elif event == "PreToolUse":
        if call.in_subagent and call.session is not None and call.agent_id not in call.sessions:
            # Its start was never registered (the warden was busy or down then): every factory call it makes would
            # be refused, so the guard registers it again on each of its calls until the warden holds it.
            said = report_subagent({"agent_id": call.agent_id, "agent_type": call.agent_type}, "SubagentStart", places)
            if said:
                print(said, file=sys.stderr)
        decision = decide.decide_tool(call)
    if isinstance(decision, Refusal):
        return _refuse(places, payload, decision.rule, decision.reason, now)
    if isinstance(decision, Rewrite):
        output = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": decision.updated_input}
        return 0, json.dumps({"hookSpecificOutput": output}), ""
    return 0, "", ""


def out_of_time(raw: str) -> Outcome:
    try:
        payload = json.loads(raw)
    # telemetry: surfaced — an unreadable input refuses like any other the guard cannot decide
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (None, "PreToolUse"):
        return 0, "", ""
    if wire.simple_way_out(payload):
        return 0, "", ""
    return 2, "", wire.one_line("guard", "the guard ran out of time to decide, so this call is refused; try again")


def _arm(places: Places) -> None:
    """Give the guard half the hook's time. Settings that cannot be read arm nothing: the guard's first step reads
    them again and refuses the call."""
    try:
        from runtime.guard.facts import read_settings

        seconds = read_settings(places).get_int("install.hook_timeout_seconds")
    # telemetry: surfaced — the rules read the settings again and refuse the call, naming why
    except Exception:  # noqa: BLE001
        return
    deadline.arm(seconds)


def main() -> int:
    raw = sys.stdin.read(_INPUT_LIMIT)
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    # Without its cgroup a session cannot be told from the owner's, and the owner's work is never blocked.
    except OSError as exc:
        print(f"factory guard: /proc/self/cgroup is unreadable ({type(exc).__name__})", file=sys.stderr)
        cgroup = ""
    places = Places.system()
    if in_factory_slice(cgroup):
        _arm(places)
    try:
        code, out, err = handle(raw, cgroup, places, Path.home(), datetime.now(UTC))
    # The time ran out while a refusal was being written: the call is refused all the same, unless it only reads.
    except deadline.OutOfTime:
        code, out, err = out_of_time(raw)
    finally:
        deadline.disarm()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
