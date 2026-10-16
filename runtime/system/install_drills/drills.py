"""The drills that prove the walls under the guard (factory spec, "Proving it"), run at every install.

Each drill does the forbidden thing the way an agent would, and passes only on evidence that the right wall stopped
it: a guard refusal must name the rule the drill is about, and a sandboxed command must be the exact one asked for,
or the drill fails as inconclusive. The installer refuses to finish while any drill fails. The drills that start a
session run a few at a time, each session in its own unit; the guard and host drills run one after another first.
The config drill makes .claude/ first, then writes each piece as a file: a folder a session could make would take one.
"""

from __future__ import annotations

import os
import shlex
import uuid
from pathlib import Path

from runtime.guard import writes
from runtime.shared import claude_accounts
from runtime.shared.paths import AGENT_USER, WARDEN_USER, stored_signin
from runtime.system import layout
from runtime.system.errors import instance
from runtime.system.install_drills.evidence import (
    POLL_S,
    Result,
    Rig,
    asked,
    breaches,
    brief,
    did,
    exact_run,
    first_line,
    outcomes,
    probe_command,
    recorded,
    refused_by,
    sandboxed_probe,
)
from runtime.system.install_steps.steps_toolchain import python_request
from runtime.warden.sessions.launch import BACKGROUNDED
from standard.services import stack

UNREACHABLE = "cannot be reached"  # how the factory command says it found no warden
SANDBOX_MARK = "software-drill-sandbox-ok"
SESSION_PROMPT, SESSION_REPLY = "This is a drill. Reply with the one word ready, and do nothing else.", "ready"
HEARD_PROMPT, HEARD_REPLY = "This is a drill. Reply with the one word heard, and do nothing else.", "heard"


def sandbox_runs(rig: Rig) -> Result:
    """The control: a session's sandbox runs a plain command. Without it, every drill below that expects a refusal
    could pass on a sandbox that runs nothing at all."""
    name = "a session's sandbox runs a plain command"
    command = f"echo {SANDBOX_MARK}"
    ran = asked(rig, command)
    run = exact_run(ran, command)
    if run is None:
        return Result(name, False, f"inconclusive: the drill session did not run the echo as asked; {did(ran)}")
    ok = not run.error and SANDBOX_MARK in run.result
    return Result(name, ok, brief(run.result))


def docker_denied(rig: Rig) -> Result:
    ran = rig.as_user(AGENT_USER, ["docker", "ps"])
    return Result("docker ps is denied", ran.code != 0, f"exit {ran.code}: {first_line(ran.out)}")


def shell_write_other_worktree(rig: Rig) -> Result:
    name = "a shell command cannot write into another worktree"
    target = rig.worktree_b / "software-drill-probe"
    command = f"touch {target}"
    run = exact_run(asked(rig, command), command)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run the touch as asked")
    written = rig.exists(target)
    return Result(name, not written, "the file was written" if written else first_line(run.result))


def edit_other_worktree(rig: Rig) -> Result:
    target = str(rig.worktree_b / "README.md")
    ran = rig.guard("Edit", {"file_path": target, "old_string": "a", "new_string": "b"})
    return Result("an edit in another worktree is refused (own-worktree)", refused_by(ran, "own-worktree"),
                  first_line(ran.out))  # fmt: skip


def claude_config_by_tool(rig: Rig) -> Result:
    target = str(rig.worktree_a / ".claude" / "settings.json")
    ran = rig.guard("Write", {"file_path": target, "content": "{}"})
    return Result("a tool cannot write Claude Code's project settings in its worktree (protected-paths)",
                  refused_by(ran, "protected-paths"), first_line(ran.out))  # fmt: skip


def claude_config_by_shell(rig: Rig) -> Result:
    """Relative paths: the session runs in a worktree made for it, where its shell starts."""
    targets = [f"write:{path.rstrip('/')}" for path in writes.CLAUDE_CONFIG]
    return sandboxed_probe(rig, "a shell command cannot write Claude Code's project config in its worktree",
                           *targets, first="mkdir -p .claude")  # fmt: skip


def secrets_unreadable(rig: Rig) -> Result:
    name = "another slot's env file, the secrets and SELF_REFLECT.md cannot be read"
    state = rig.places.state
    # Every slot's env file lies in the stack user's folder of slots, which no other user may even list.
    targets = [f"dir:{rig.places.stack_slots}", f"file:{layout.secrets(state) / 'claude.token'}",
               f"dir:{claude_accounts.folder(layout.secrets(state))}",
               f"file:{rig.places.totp_secret}", f"file:{rig.places.services / stack.ENV_FILE}",
               f"file:{rig.places.reflections}", f"file:{state / 'reflections.json'}"]  # fmt: skip
    plain = rig.as_user(AGENT_USER, probe_command(rig, *targets))
    inside = sandboxed_probe(rig, name, f"file:{stored_signin(layout.AGENT_HOME)}",
                              "env:CLAUDE_CODE_OAUTH_TOKEN")  # fmt: skip
    breached = breaches(plain.out)
    ok = not breached and len(outcomes(plain.out)) == len(targets) and inside.ok
    return Result(name, ok, f"as the agent: {', '.join(outcomes(plain.out).values())}; sandboxed: {inside.evidence}")


def sockets_refused(rig: Rig) -> Result:
    return sandboxed_probe(rig, "the Docker and warden sockets are refused in the sandbox",
                            f"unix:{rig.places.socket}", "unix:/run/docker.sock")  # fmt: skip


def hosts_refused(rig: Rig) -> Result:
    port = rig.settings.get_int("dashboard.port")
    return sandboxed_probe(rig, "a host off the list and the host's localhost are refused",
                            "tcp:example.com:443", f"tcp:127.0.0.1:{port}")  # fmt: skip


def background_session(rig: Rig) -> Result:
    """A real background session, started as the warden starts one: `claude --bg` names it, its record in the
    agent's ~/.claude/sessions names it, it answers (so the daemon signed it in), `claude stop` ends it,
    `claude respawn` brings it back under its own id, as the warden resumes one, and a message handed over as the
    warden hands one gets its answer. Each is Claude Code's own, which the warden uses and no version promises to
    keep."""
    name = "a background session starts, is recorded by name, answers, stops, respawns and hears a message"
    tag = f"drill-session-{uuid.uuid4().hex[:8]}"
    ran = rig.real_session(tag, SESSION_PROMPT)
    started = BACKGROUNDED.search(ran.out)
    if ran.code != 0 or started is None:
        return Result(name, False, f"claude --bg printed no 'backgrounded · <id>' line (exit {ran.code}): "
                                   f"{first_line(ran.out)}")  # fmt: skip
    short, limit = started.group(1), rig.settings.get_int("install.drill_seconds")
    begun = rig.now()
    found = recorded(rig, short, begun + limit)
    if found is None:
        rig.stop_session(short)
        return Result(name, False, f"no record in the agent's ~/.claude/sessions names session {short}")
    sid, named = found
    while not (said := rig.session_said(sid)) and rig.now() - begun <= limit:
        rig.wait(POLL_S)
    failed = _stop_and_respawn(rig, short, sid, begun + limit)
    if failed:
        return Result(name, False, failed)
    if named != tag:
        return Result(name, False, f"its record names it {named!r}, not {tag}: the warden binds a start by its name")
    if SESSION_REPLY not in said.lower():
        return Result(name, False, f"it never answered as asked: {first_line(said) or 'it said nothing'}")
    done = f"started {short}, recorded as {tag}, answered {first_line(said)!r}, stopped, respawned, heard"
    return Result(name, True, done)


def _stop_and_respawn(rig: Rig, short: str, sid: str, deadline: float) -> str:
    """Why `claude stop` left the session running, `claude respawn` did not bring it back under its id, or it did not
    answer a message; "" when all did as the warden needs. The respawned session is stopped again."""
    stopped = rig.stop_session(short)
    while sid in rig.session_records() and rig.now() <= deadline:
        rig.wait(POLL_S)
    if sid in rig.session_records():
        return f"claude stop {short} left it running: {first_line(stopped.out)}"
    back = rig.warden_command(["claude", "respawn", short])
    while sid not in rig.session_records() and rig.now() <= deadline:
        rig.wait(POLL_S)
    if sid not in rig.session_records():
        rig.stop_session(short)
        return f"claude respawn {short} did not bring it back under its id: {first_line(back.out)}"
    missed = _unheard(rig, sid, deadline)
    rig.stop_session(short)
    return missed


def _unheard(rig: Rig, sid: str, deadline: float) -> str:
    """Why a message handed to the session as the warden hands one got no answer; "" when it got one."""
    handed = rig.message_session(sid, HEARD_PROMPT)
    while HEARD_REPLY not in rig.session_said(sid).lower() and rig.now() <= deadline:
        rig.wait(POLL_S)
    if HEARD_REPLY in rig.session_said(sid).lower():
        return ""
    return f"a message handed over as the warden does got no answer: {first_line(handed.out or rig.session_said(sid))}"


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
            rig.wait(POLL_S)
        seen = rig.now()
        # A tick may have begun just before claude appeared, so the warden has until the end of the next one.
        limit = 2 * rig.settings.get_int("watch.tick_seconds")
        while rig.now() - seen <= limit:
            if not rig.claude_pids(unit):
                return Result(name, True, f"stopped {rig.now() - seen:.0f} s after it started")
            rig.wait(POLL_S)
        return Result(name, False, f"still running {limit} s after it started")
    finally:
        rig.stop(unit)


def bubblewrap_missing(rig: Rig) -> Result:
    name = "with bubblewrap missing, a session runs no command"
    command = " ".join(probe_command(rig, "env:PATH"))
    # The machine without bubblewrap, for this one session: the program is there, and no process of the session may
    # reach it.
    ran = rig.sandboxed(command, properties=(f"InaccessiblePaths={layout.BWRAP}",))
    run = exact_run(ran, command)
    if run is not None and outcomes(run.result).get("env:PATH") == "set":
        return Result(name, False, "the command ran without the sandbox")
    return Result(name, True, f"exit {ran.code}: {first_line(run.result if run else ran.out)}")


def factory_alone(rig: Rig) -> Result:
    ran = rig.guard("Bash", {"command": "factory status && touch x"})
    return Result("factory status && touch x is refused (factory-alone)", refused_by(ran, "factory-alone"),
                  first_line(ran.out))  # fmt: skip


def finished_subagent(rig: Rig) -> Result:
    ran = rig.guard("SendMessage", {"to": rig.finished_agent, "message": "one more thing"})
    return Result(
        "a finished intake subagent cannot be messaged", refused_by(ran, "resume-subagent"), first_line(ran.out)
    )


def other_branch(rig: Rig) -> Result:
    ran = rig.guard("Bash", {"command": f"git update-ref refs/heads/{rig.branch_b} HEAD"})
    return Result("moving another session's branch is refused (own-branch)", refused_by(ran, "own-branch"),
                  first_line(ran.out))  # fmt: skip


def factory_in_sandbox(rig: Rig) -> Result:
    """The sandbox lets out only the bare word `factory`, typed alone, so that is what the session types."""
    name = "factory status reaches the warden from inside a session"
    command = "factory status"
    run = exact_run(asked(rig, command), command)
    outside = rig.as_user(AGENT_USER, [str(layout.FACTORY_BIN), "status"], view=True)
    if run is None:
        return Result(name, False, "inconclusive: the drill session did not run factory status as asked")
    ok = UNREACHABLE not in run.result and UNREACHABLE not in outside.out
    return Result(name, ok, f"inside: {first_line(run.result)}; outside: {first_line(outside.out)}")


def path_shadow(rig: Rig) -> Result:
    """The sandbox lets out the bare word `factory`; a `factory` of the agent's own, put first on PATH by an earlier
    call, must never run outside it and reach the warden."""
    name = "a factory of the agent's own, first on PATH, never reaches the warden"
    probe = probe_command(rig)
    setup = (
        f"mkdir -p drill-bin && cp {probe[1]} drill-bin/factory && chmod +x drill-bin/factory && "
        'export PATH="$PWD/drill-bin:$PATH"'
    )
    call = f"factory {' '.join(probe[2:])} unix:{rig.places.socket}"
    ran = asked(rig, setup, call)
    run = exact_run(ran, call)
    if run is None:
        return Result(name, False, f"inconclusive: the drill session did not run the shadow as asked; {did(ran)}")
    breached = breaches(run.result)
    return Result(name, not breached, "the shadow reached the warden" if breached else first_line(run.result))


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
    ran = asked(rig, *commands)
    runs = [(command, exact_run(ran, command)) for command in commands]
    done = [(command, run) for command, run in runs if run is not None]
    if len(done) < len(commands):
        return Result(name, False, f"inconclusive: the drill session did not run every command as asked; {did(ran)}")
    failed = [f"{command}: {brief(run.result)}" for command, run in done if run.error]
    found = next((first_line(run.result) for command, run in done if command == find and not run.error), "")
    real = os.path.realpath(found) if found else ""
    if found and not (real.startswith(f"{layout.TOOLCHAIN_PYTHON}/") or str(Path(real).parent) in layout.SYSTEM_PATH):
        failed.append(f"{find} found {found}, which is {real}, not the toolchain's copy")
    evidence = "; ".join(failed or [f"{command}: {first_line(run.result)}" for command, run in done])
    return Result(name, not failed, evidence)


def stack_not_root(rig: Rig) -> Result:
    name = "scripts/dev cannot reach the host as root"
    info = rig.as_user(layout.STACK_USER, ["docker", "info", "--format", "{{json .SecurityOptions}}"])
    system = rig.as_user(layout.STACK_USER, probe_command(rig, "unix:/run/docker.sock"))
    ok = info.code == 0 and "rootless" in info.out and not breaches(system.out)
    return Result(name, ok, f"security options {first_line(info.out)}; system socket: {first_line(system.out)}")


# Tailscale's own address on every tailnet; its DNS answers there over TCP too, so a wall that lets the tailnet through
# shows as a connection.
TAILNET_PROBE = "100.100.100.100"


def stack_off_tailnet(rig: Rig) -> Result:
    """Agent-written code runs as the stack user, in its image builds and its containers: none reaches the tailnet,
    where prod's pgweb and Postgres answer, or the dashboard."""
    name = "the stack user and a slot's container reach neither the tailnet nor the dashboard"
    port = rig.settings.get_int("dashboard.port")
    plain = rig.as_user(layout.STACK_USER, probe_command(rig, f"tcp:{TAILNET_PROBE}:53", f"tcp:127.0.0.1:{port}"))
    image = rig.as_user(layout.STACK_USER, ["docker", "inspect", "--format", "{{.Config.Image}}", "factory-postgres"])
    seconds = rig.settings.get_int("watch.http_timeout_seconds")
    target = f"tcp:{TAILNET_PROBE}:53"
    script = (f"if timeout {seconds} bash -c 'exec 3<>/dev/tcp/{TAILNET_PROBE}/53'; then echo '{target} -> connected'; "
              f"else echo '{target} -> refused'; fi")  # fmt: skip
    inside = rig.as_user(layout.STACK_USER, ["docker", "run", "--rm", "--entrypoint", "bash", image.out.strip(), "-c",
                                             script])  # fmt: skip
    found = {**outcomes(plain.out), **{f"container {t}": o for t, o in outcomes(inside.out).items()}}
    ok = image.code == 0 and len(found) == 3 and not breaches(plain.out) and not breaches(inside.out)
    return Result(name, ok, "; ".join(f"{t} -> {o}" for t, o in found.items()) or first_line(plain.out + inside.out))


def shared_services_answer(rig: Rig) -> Result:
    """The stack user's Docker publishes GlitchTip and Langfuse on the loopback, where the slots' stacks and the
    warden reach them."""
    targets = [f"get:{layout.GLITCHTIP_HEALTH}", f"get:{layout.LANGFUSE_HEALTH}"]
    ran = rig.as_user(layout.STACK_USER, probe_command(rig, *targets))
    found = outcomes(ran.out)
    ok = len(found) == len(targets) and set(found.values()) == {"answered 200"}
    evidence = "; ".join(f"{target} -> {outcome}" for target, outcome in found.items()) or first_line(ran.out)
    return Result("GlitchTip and Langfuse answer the stack user on the loopback", ok, evidence)


def factory_errors_walled_off(rig: Rig) -> Result:
    """The factory's own GlitchTip answers the warden, which sends it the factory's errors. Neither user that runs
    agent code reaches its port or reads its settings, and the stack user's Docker does not run it."""
    name = "the factory's own GlitchTip answers the warden, and no user that runs agent code reaches it"
    warden = outcomes(rig.as_user(WARDEN_USER, probe_command(rig, f"get:{instance.HEALTH}")).out)
    targets = [f"tcp:127.0.0.1:{instance.PORT}", f"file:{rig.places.errors / instance.ENV_FILE}"]
    others = {user: rig.as_user(user, probe_command(rig, *targets)).out for user in (AGENT_USER, layout.STACK_USER)}
    inspect = rig.as_user(layout.STACK_USER, ["docker", "inspect", "--format", "{{.Name}}", instance.CONTAINER])
    walled = all(len(outcomes(out)) == len(targets) and not breaches(out) for out in others.values())
    ok = set(warden.values()) == {"answered 200"} and walled and inspect.code != 0
    said = "; ".join(
        f"as {user}: {', '.join(outcomes(out).values()) or first_line(out)}" for user, out in others.items()
    )
    runs = "runs it" if inspect.code == 0 else "does not run it"
    return Result(
        name, ok, f"as the warden: {', '.join(warden.values()) or 'nothing'}; {said}; the stack user's Docker {runs}"
    )


def dashboard_unreachable(rig: Rig) -> Result:
    port = rig.settings.get_int("dashboard.port")
    start = rig.now()
    # The dashboard was just restarted: as root it must answer before the agent's refusal means anything.
    while (as_root := rig.connect("127.0.0.1", port)) != "connected":
        if rig.now() - start > rig.settings.get_int("install.drill_seconds"):
            break
        rig.wait(POLL_S)
    agent = rig.as_user(AGENT_USER, probe_command(rig, f"tcp:127.0.0.1:{port}"))
    ok = as_root == "connected" and not breaches(agent.out) and bool(outcomes(agent.out))
    return Result("the dashboard is unreachable from the agent user", ok,
                  f"as root: {as_root}; as the agent: {first_line(agent.out)}")  # fmt: skip


def warden_runs_commands(rig: Rig) -> Result:
    """The warden starts each session and stops one through its template unit: a command run there as the agent must
    come back with what it printed and how it ended."""
    good = rig.warden_command(["git", "rev-parse", "--is-inside-work-tree"])
    bad = rig.warden_command(["git", "rev-parse", "--verify", "--quiet", "refs/heads/software-drill-none"])
    ok = (good.code, good.out) == (0, "true\n") and bad.code == 1
    return Result("the warden runs a command as the agent and reads what it printed and how it ended", ok,
                  f"a command: exit {good.code}, {first_line(good.out)!r}; a failing one: exit {bad.code}")  # fmt: skip


def check_walled_in(rig: Rig) -> Result:
    """The product code a check runs (here its deps verb) is walled into its checkout by the check-run unit."""
    name = "a check's product code reads no sign-in or other worktree, and cannot change the shared git folder"
    targets = [f"file:{stored_signin(layout.AGENT_HOME)}", f"dir:{rig.worktree_b}",
               "env:CREDENTIALS_DIRECTORY", "env:CLAUDE_CODE_OAUTH_TOKEN"]  # fmt: skip
    checkout, ran = rig.warden_check(probe_command(rig, *targets, f"write:{Path('.git') / 'config'}"))
    found = outcomes(ran.out)
    ok = len(found) == len(targets) + 1 and not breaches(ran.out)
    return Result(name, ok, "; ".join(f"{t} -> {o}" for t, o in found.items()) or f"{checkout}: {first_line(ran.out)}")
