"""What a drill acts through and what it reads its evidence from: the rig, the guard's refusal naming its rule, a
drill session's Bash calls with their results, and the probe's ``target -> outcome`` lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runtime.guard.wire import REFUSED as GUARD_REFUSAL
from runtime.shared.paths import Places
from runtime.shared.settings import Settings
from runtime.system import layout
from runtime.system.drill_probe import BREACHES


@dataclass(frozen=True, slots=True)
class Ran:
    code: int
    out: str


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    ok: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ToolRun:
    command: str
    result: str
    error: bool


class Rig(Protocol):
    """What a drill may do on the machine; the installer's rig does it for real, a test's rig pretends."""

    places: Places
    settings: Settings
    worktree_a: Path  # the guard drills' session's worktree, where the agent's plain commands run
    worktree_b: Path  # another session's
    branch_b: str
    finished_agent: str  # a finished intake subagent in the drill session's transcript

    def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran: ...
    def as_daemon(self, argv: list[str]) -> Ran:
        """``argv`` as the agent user, inside the walls the installed agents' daemon unit puts round its sessions."""
        ...

    def guard(self, tool: str, tool_input: dict[str, Any]) -> Ran: ...
    def sandboxed(
        self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()
    ) -> Ran: ...
    def exists(self, path: Path) -> bool: ...
    def connect(self, host: str, port: int) -> str: ...
    def background(self, user: str, argv: list[str]) -> str: ...
    def claude_pids(self, unit: str) -> list[int]: ...
    def stop(self, unit: str) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def now(self) -> float: ...
    def warden_command(self, argv: list[str]) -> Ran: ...
    def real_session(self, name: str, prompt: str, **more: str) -> Ran:
        """A session started as the warden starts one, with ``more`` in its request; what `claude --bg` printed."""
        ...

    def session_records(self) -> dict[str, str]:
        """Each live session the agent's ~/.claude/sessions records, as the backstop reads it: its id to its name."""
        ...

    def session_said(self, session_id: str) -> str:
        """What the session said first, from its transcript; empty before it said anything."""
        ...

    def stop_session(self, short_id: str) -> Ran: ...
    def message_session(self, session_id: str, text: str) -> Ran: ...
    def warden_check(self, probe: list[str]) -> tuple[Path, Ran]:
        """A check the warden runs, through the check-run unit, on a scratch checkout in the agent's home whose
        ``scripts/dev deps`` runs ``probe`` and fails with what it printed; that checkout and the check's report."""
        ...


POLL_S = 1.0


def recorded(rig: Rig, short: str, deadline: float) -> tuple[str, str] | None:
    """The full id and recorded name of the session `claude --bg` printed as ``short``; None if none by ``deadline``."""
    while True:
        found = [(sid, named) for sid, named in rig.session_records().items() if sid.startswith(short)]
        if found or rig.now() > deadline:
            return found[0] if found else None
        rig.wait(POLL_S)


def refused_by(ran: Ran, rule: str) -> bool:
    """The guard refused, and named this rule: a refusal by any other rule proves nothing about this one."""
    return ran.code == 2 and f"({rule})" in ran.out


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def tool_runs(stream: str) -> list[ToolRun]:
    """Every Bash call and its result in a `claude -p --output-format stream-json` run."""
    asked: dict[str, str] = {}
    runs: list[ToolRun] = []
    for line in stream.splitlines():
        try:
            entry = json.loads(line)
        # telemetry: expected — the stream also carries lines that are not JSON, such as warnings
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                asked[str(block.get("id"))] = str((block.get("input") or {}).get("command", ""))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in asked:
                runs.append(ToolRun(asked[str(block["tool_use_id"])], _text(block.get("content")),
                                    bool(block.get("is_error"))))  # fmt: skip
    return runs


def exact_run(ran: Ran, command: str) -> ToolRun | None:
    """The run of exactly ``command``; None when the model ran something else, or nothing."""
    return next((r for r in tool_runs(ran.out) if r.command.strip() == command.strip()), None)


def outcomes(text: str) -> dict[str, str]:
    """The probe's ``target -> outcome`` lines."""
    found = {}
    for line in text.splitlines():
        target, sep, outcome = line.strip().partition(" -> ")
        if sep:
            found[target] = outcome.strip()
    return found


def breaches(text: str) -> list[str]:
    return [target for target, outcome in outcomes(text).items() if outcome in BREACHES]


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(nothing)")[:200]


def brief(text: str) -> str:
    """Every line of a result, joined: an error's first line ("Exit code 1") alone says nothing of its cause."""
    return " / ".join(line.strip() for line in text.splitlines() if line.strip())[:400] or "(nothing)"


def did(ran: Ran) -> str:
    """What a drill session did instead of what it was asked: the commands it ran, or its last words."""
    runs = tool_runs(ran.out)
    if runs:
        return "it ran: " + "; ".join(f"{r.command[:120]} -> {brief(r.result)[:160]}" for r in runs[:3])
    said = [line for line in ran.out.splitlines() if line.strip()]
    return "it ran nothing; its output ends: " + brief("\n".join(said[-3:]))[:300]


def probe_command(rig: Rig, *targets: str) -> list[str]:
    seconds = str(rig.settings.get_int("watch.http_timeout_seconds"))
    return [layout.PYTHON, str(rig.places.install / "runtime" / "system" / "drill_probe.py"), "--seconds", seconds,
            *targets]  # fmt: skip


def sandboxed_probe(rig: Rig, name: str, *targets: str, first: str = "") -> Result:
    command = " && ".join(part for part in (first, " ".join(probe_command(rig, *targets))) if part)
    ran = asked(rig, command)
    run = exact_run(ran, command)
    if run is None:
        return Result(name, False, f"inconclusive: the drill session did not run the probe as asked; {did(ran)}")
    breached = breaches(run.result)
    # The probe must report each target, or the guard must refuse the call: a bare failure (a sandbox that runs
    # nothing at all) proves nothing.
    answered = bool(outcomes(run.result)) or GUARD_REFUSAL in run.result
    evidence = f"breached {', '.join(breached)}" if breached else brief(run.result)
    return Result(name, answered and not breached, evidence)


def asked(rig: Rig, *commands: str) -> Ran:
    """A drill session that runs ``commands``; once more when the first ran none of them: a model that ran out of time
    deciding has proved nothing either way."""
    ran = rig.sandboxed(*commands)
    if any(exact_run(ran, command) is not None for command in commands):
        return ran
    return rig.sandboxed(*commands)
