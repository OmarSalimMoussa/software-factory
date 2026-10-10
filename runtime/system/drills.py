"""The drills that prove the walls under the guard (factory spec, "Proving it"), run at every install.

Each drill does the forbidden thing the way an agent would, and passes only on evidence that the right wall stopped
it: a guard refusal must name the rule the drill is about, and a sandboxed command must be the exact one asked for,
or the drill fails as inconclusive. The installer refuses to finish while any drill fails.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runtime.shared.paths import AGENT_USER, Places
from runtime.shared.settings import Settings
from runtime.system import layout
from runtime.system.drill_probe import BREACHES
from runtime.system.steps_toolchain import python_request
from standard.services import stack

UNREACHABLE = "cannot be reached"  # how the factory command says it found no warden
_POLL_S = 1.0


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
    worktree_a: Path  # the drill session's own worktree
    worktree_b: Path  # another session's
    branch_b: str
    finished_agent: str  # a finished intake subagent in the drill session's transcript

    def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran: ...
    def guard(self, tool: str, tool_input: dict[str, Any]) -> Ran: ...
    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None) -> Ran: ...
    def exists(self, path: Path) -> bool: ...
    def connect(self, host: str, port: int) -> str: ...
    def background(self, user: str, argv: list[str]) -> str: ...
    def claude_pids(self, unit: str) -> list[int]: ...
    def stop(self, unit: str) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def now(self) -> float: ...


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


def _breached(text: str) -> list[str]:
    return [target for target, outcome in outcomes(text).items() if outcome in BREACHES]


def _first(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "(nothing)")[:200]


def _probe(rig: Rig, *targets: str) -> list[str]:
    seconds = str(rig.settings.get_int("watch.http_timeout_seconds"))
    return [layout.PYTHON, str(rig.places.install / "runtime" / "system" / "drill_probe.py"), "--seconds", seconds,
            *targets]  # fmt: skip


def _sandboxed_probe(rig: Rig, name: str, *targets: str) -> Result:
    command = " ".join(_probe(rig, *targets))
    run = exact_run(rig.sandboxed(command), command)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run the probe as asked")
    breached = _breached(run.result)
    # A refused call (the guard's, or the sandbox stopping python itself) answers too; silence proves nothing.
    answered = bool(outcomes(run.result)) or run.error
    evidence = f"breached {', '.join(breached)}" if breached else _first(run.result)
    return Result(name, answered and not breached, evidence)


def docker_denied(rig: Rig) -> Result:
    ran = rig.as_user(AGENT_USER, ["docker", "ps"])
    return Result("docker ps is denied", ran.code != 0, f"exit {ran.code}: {_first(ran.out)}")


def shell_write_other_worktree(rig: Rig) -> Result:
    name = "a shell command cannot write into another worktree"
    target = rig.worktree_b / "software-drill-probe"
    command = f"touch {target}"
    run = exact_run(rig.sandboxed(command), command)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run the touch as asked")
    written = rig.exists(target)
    return Result(name, not written, "the file was written" if written else _first(run.result))


def edit_other_worktree(rig: Rig) -> Result:
    target = str(rig.worktree_b / "README.md")
    ran = rig.guard("Edit", {"file_path": target, "old_string": "a", "new_string": "b"})
    return Result("an edit in another worktree is refused (own-worktree)", refused_by(ran, "own-worktree"),
                  _first(ran.out))  # fmt: skip


def secrets_unreadable(rig: Rig) -> Result:
    name = "another slot's env file and the secrets cannot be read"
    state = rig.places.state
    # Every slot's env file lies in the stack user's folder of slots, which no other user may even list.
    targets = [f"dir:{rig.places.stack_slots}", f"file:{layout.secrets(state) / 'claude.token'}",
               f"file:{rig.places.totp_secret}", f"file:{rig.places.services / stack.ENV_FILE}"]  # fmt: skip
    plain = rig.as_user(AGENT_USER, _probe(rig, *targets))
    inside = _sandboxed_probe(rig, name, f"file:{layout.AGENT_HOME}/.claude/.credentials.json",
                              "env:CLAUDE_CODE_OAUTH_TOKEN")  # fmt: skip
    breached = _breached(plain.out)
    ok = not breached and len(outcomes(plain.out)) == len(targets) and inside.ok
    return Result(name, ok, f"as the agent: {', '.join(outcomes(plain.out).values())}; sandboxed: {inside.evidence}")


def sockets_refused(rig: Rig) -> Result:
    return _sandboxed_probe(rig, "the Docker and warden sockets are refused in the sandbox",
                            f"unix:{rig.places.socket}", "unix:/run/docker.sock")  # fmt: skip


def hosts_refused(rig: Rig) -> Result:
    port = rig.settings.get_int("dashboard.port")
    return _sandboxed_probe(rig, "a host off the list and the host's localhost are refused",
                            "tcp:example.com:443", f"tcp:127.0.0.1:{port}")  # fmt: skip


def backstop_stops_claude(rig: Rig) -> Result:
    """`python3 -c` starts a renamed claude that waits on its input forever; the warden must stop it."""
    name = "a claude started by python3 -c is stopped within one tick"
    code = (
        "import os, subprocess; r, w = os.pipe(); subprocess.run(['drill-renamed', '-p', '--input-format', "
        f"'stream-json', '--output-format', 'stream-json', '--verbose'], executable={str(layout.CLAUDE_BIN)!r}, "
        "stdin=r)"
    )
    unit = rig.background(AGENT_USER, [layout.PYTHON, "-c", code])
    try:
        start = rig.now()
        while not rig.claude_pids(unit):
            if rig.now() - start > rig.settings.get_int("install.drill_seconds"):
                return Result(name, False, "inconclusive: the drill's claude never started")
            rig.wait(_POLL_S)
        seen = rig.now()
        # A tick may have begun just before claude appeared, so the warden has until the end of the next one.
        limit = 2 * rig.settings.get_int("watch.tick_seconds")
        while rig.now() - seen <= limit:
            if not rig.claude_pids(unit):
                return Result(name, True, f"stopped {rig.now() - seen:.0f} s after it started")
            rig.wait(_POLL_S)
        return Result(name, False, f"still running {limit} s after it started")
    finally:
        rig.stop(unit)


def bubblewrap_missing(rig: Rig) -> Result:
    name = "with bubblewrap missing, a session runs no command"
    command = " ".join(_probe(rig, "env:PATH"))
    ran = rig.sandboxed(command, extra={"sandbox": {"bwrapPath": "/nonexistent/bwrap"}})
    run = exact_run(ran, command)
    if run is not None and outcomes(run.result).get("env:PATH") == "set":
        return Result(name, False, "the command ran without the sandbox")
    return Result(name, True, f"exit {ran.code}: {_first(run.result if run else ran.out)}")


def factory_alone(rig: Rig) -> Result:
    ran = rig.guard("Bash", {"command": "factory status && touch x"})
    return Result("factory status && touch x is refused (factory-alone)", refused_by(ran, "factory-alone"),
                  _first(ran.out))  # fmt: skip


def finished_subagent(rig: Rig) -> Result:
    ran = rig.guard("SendMessage", {"to": rig.finished_agent, "message": "one more thing"})
    return Result("a finished intake subagent cannot be messaged", refused_by(ran, "resume-subagent"), _first(ran.out))


def other_branch(rig: Rig) -> Result:
    ran = rig.guard("Bash", {"command": f"git update-ref refs/heads/{rig.branch_b} HEAD"})
    return Result("moving another session's branch is refused (own-branch)", refused_by(ran, "own-branch"),
                  _first(ran.out))  # fmt: skip


def factory_in_sandbox(rig: Rig) -> Result:
    """The sandbox lets out only the bare word `factory`, typed alone, so that is what the session types."""
    name = "factory status reaches the warden from inside a session"
    command = "factory status"
    run = exact_run(rig.sandboxed(command), command)
    outside = rig.as_user(AGENT_USER, [str(layout.FACTORY_BIN), "status"], view=True)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run factory status as asked")
    ok = UNREACHABLE not in run.result and UNREACHABLE not in outside.out
    return Result(name, ok, f"inside: {_first(run.result)}; outside: {_first(outside.out)}")


def path_shadow(rig: Rig) -> Result:
    """The sandbox lets out the bare word `factory`; a `factory` of the agent's own, put first on PATH by an earlier
    call, must never run outside it and reach the warden."""
    name = "a factory of the agent's own, first on PATH, never reaches the warden"
    probe = _probe(rig)
    setup = (
        f"mkdir -p drill-bin && cp {probe[1]} drill-bin/factory && chmod +x drill-bin/factory && "
        'export PATH="$PWD/drill-bin:$PATH"'
    )
    call = f"factory {' '.join(probe[2:])} unix:{rig.places.socket}"
    run = exact_run(rig.sandboxed(setup, call), call)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run the shadow as asked")
    breached = _breached(run.result)
    return Result(name, not breached, "the shadow reached the warden" if breached else _first(run.result))


def toolchain_answers(rig: Rig) -> Result:
    """Each program of the product's toolchain runs inside a session, and uv finds the Python the product asks for
    where the install put it, which also takes the uv cache the session may write."""
    name = "the product's toolchain and its Python answer inside a session"
    toolchain = [str(program) for program in rig.settings.get_list("install.toolchain")]
    commands = [f"{program} --version" for program in toolchain]
    request = python_request(rig.worktree_a)
    find = f"uv python find {shlex.quote(request)}" if request and "uv" in toolchain else ""
    if find:
        commands.append(find)
    if not commands:
        return Result(name, True, "the product needs no toolchain and no Python")
    ran = rig.sandboxed(*commands)
    runs = [(command, exact_run(ran, command)) for command in commands]
    done = [(command, run) for command, run in runs if run is not None]
    if len(done) < len(commands):
        return Result(name, False, "inconclusive: the drill session did not run every command as asked")
    failed = [f"{command}: {_first(run.result)}" for command, run in done if run.error]
    found = next((_first(run.result) for command, run in done if command == find and not run.error), "")
    if found and not (found.startswith(f"{layout.TOOLCHAIN_PYTHON}/") or str(Path(found).parent) in layout.SYSTEM_PATH):
        failed.append(f"{find} found {found}, not the toolchain's copy")
    evidence = "; ".join(failed or [f"{command}: {_first(run.result)}" for command, run in done])
    return Result(name, not failed, evidence)


def stack_not_root(rig: Rig) -> Result:
    name = "scripts/dev cannot reach the host as root"
    info = rig.as_user(layout.STACK_USER, ["docker", "info", "--format", "{{json .SecurityOptions}}"])
    system = rig.as_user(layout.STACK_USER, _probe(rig, "unix:/run/docker.sock"))
    ok = info.code == 0 and "rootless" in info.out and not _breached(system.out)
    return Result(name, ok, f"security options {_first(info.out)}; system socket: {_first(system.out)}")


def shared_services_answer(rig: Rig) -> Result:
    """The stack user's Docker publishes GlitchTip and Langfuse on the loopback, where the slots' stacks and the
    warden reach them."""
    ran = rig.as_user(layout.STACK_USER, _probe(rig, f"get:{layout.GLITCHTIP_HEALTH}", f"get:{layout.LANGFUSE_HEALTH}"))
    found = outcomes(ran.out)
    ok = len(found) == 2 and set(found.values()) == {"answered 200"}
    evidence = "; ".join(f"{target} -> {outcome}" for target, outcome in found.items()) or _first(ran.out)
    return Result("GlitchTip and Langfuse answer the stack user on the loopback", ok, evidence)


def dashboard_unreachable(rig: Rig) -> Result:
    port = rig.settings.get_int("dashboard.port")
    start = rig.now()
    # The dashboard was just restarted: as root it must answer before the agent's refusal means anything.
    while (as_root := rig.connect("127.0.0.1", port)) != "connected":
        if rig.now() - start > rig.settings.get_int("install.drill_seconds"):
            break
        rig.wait(_POLL_S)
    agent = rig.as_user(AGENT_USER, _probe(rig, f"tcp:127.0.0.1:{port}"))
    ok = as_root == "connected" and not _breached(agent.out) and bool(outcomes(agent.out))
    return Result("the dashboard is unreachable from the agent user", ok,
                  f"as root: {as_root}; as the agent: {_first(agent.out)}")  # fmt: skip


DRILLS: tuple[Callable[[Rig], Result], ...] = (
    docker_denied,
    shell_write_other_worktree,
    edit_other_worktree,
    secrets_unreadable,
    sockets_refused,
    hosts_refused,
    backstop_stops_claude,
    bubblewrap_missing,
    factory_alone,
    finished_subagent,
    other_branch,
    factory_in_sandbox,
    path_shadow,
    toolchain_answers,
    stack_not_root,
    shared_services_answer,
    dashboard_unreachable,
)


def run_all(rig: Rig) -> list[Result]:
    results = []
    for drill in DRILLS:
        try:
            results.append(drill(rig))
        # A drill that breaks has proved nothing: it fails, named, and the others still run.
        except Exception as exc:  # noqa: BLE001
            results.append(Result(drill.__name__, False, f"the drill broke: {type(exc).__name__}: {exc}"))
    return results
