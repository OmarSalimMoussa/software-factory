"""How the warden starts sessions, and the real Launcher under it.

The warden runs anything only by writing a request to public/launch/<id>.json and starting one of the three template
units polkit lets it start. The one other unit action polkit allows it is a restart of the agents' Claude daemon, when
the owner switches Claude accounts.

``software-agent-run@<id>.service`` runs as the agent user: a new session (``kind: session``) or one short command
(``kind: command``). ``software-stack-run@<id>.service`` runs ``scripts/dev`` as the stack user (``kind: stack``).
``software-check-run@<id>.service`` runs `factory check` as the agent user, walled into the checkout (``kind: check``).
The units write their output to ``runs/<id>.log`` and their exit status to ``runs/<id>.exit`` in the warden's state
(systemd opens both as root, so the unit's own user never reaches the warden's state).
"""

from __future__ import annotations

import json
import pwd
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.models import PENDING, WORKING, Session
from runtime.shared.paths import AGENT_USER, UPGRADE_UNIT, Places, stored_signin
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.system.layout import DAEMON_UNIT
from runtime.warden.context import Context, StackResult
from runtime.warden.sessions import reap, registry, session_settings
from runtime.warden.spend import cost

AGENT_UNIT = "software-agent-run@{}.service"
STACK_UNIT = "software-stack-run@{}.service"
CHECK_UNIT = "software-check-run@{}.service"
CHECK_REQUEST = "check-"
# What `claude --bg` prints for the session it started; the install drills it on the Claude Code it installs.
BACKGROUNDED = re.compile(r"^backgrounded · ([0-9a-f]{8})\b", re.MULTILINE)

Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


class LaunchError(Exception):
    """A unit could not be started, or the command it ran failed; the message says which and why."""


def forget_checks(places: Places) -> None:
    """Drops the check requests and results a warden left: none has a reader, and a restarted check then finds none."""
    for folder in (places.state / "public" / "launch", places.state / "runs"):
        for path in folder.glob(f"{CHECK_REQUEST}*"):
            path.unlink()


def agent_home() -> Path:
    return Path(pwd.getpwnam(AGENT_USER).pw_dir)


def agent_uid() -> int | None:
    try:
        return pwd.getpwnam(AGENT_USER).pw_uid
    # telemetry: expected — an install not finished: there is no agent user to look for
    except KeyError:
        return None


# What every session runs under, as flag and launch key.
CARRIED = (("--remote-control", "name"), ("--model", "model"), ("--effort", "effort"), ("--agent", "agent"),
           ("--settings", "settings"), ("--agents", "agents"))  # fmt: skip


def _carried(launch: dict[str, Any]) -> list[str]:
    return [word for flag, key in CARRIED for word in (flag, launch[key])]


def session_argv(launch: dict[str, Any]) -> list[str]:
    """The `claude` command a session request names; the agent-run unit's runner executes exactly this. One that names
    a stopped session to ``resume`` starts a copy of it, with its whole conversation, under these options."""
    if launch.get("resume"):
        argv = ["claude", "--bg", "--resume", launch["resume"], "--name", launch["name"], *_carried(launch)]
    else:
        argv = ["claude", "--bg", "--name", launch["name"], *_carried(launch)]
        if launch["worktree"]:
            argv += ["--worktree", launch["worktree"]]
    # Without the --, a prompt that starts with a dash would be read as a flag.
    return [*argv, "--", launch["prompt"]]


def _systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *argv], capture_output=True, text=True, check=False)


class Units:
    """Launcher through the two template units and the daemon's restart. ``run`` runs one systemctl command (a test
    passes a fake)."""

    def __init__(self, places: Places, settings: Callable[[], Settings], run: Run = _systemctl) -> None:
        self.places = places
        self.settings = settings
        self.run = run

    def _request(self, request_id: str, body: dict[str, Any]) -> None:
        store.write_json(self.places.state / "public" / "launch" / f"{request_id}.json", body)

    def _wait(self, unit: str, request_id: str) -> tuple[int, str]:
        """Start ``unit``, wait until it ends, and read what it printed and how it exited."""
        runs = self.places.state / "runs"
        done = self.run(["--wait", "start", unit])
        try:
            status = (runs / f"{request_id}.exit").read_text().strip()
            output = (runs / f"{request_id}.log").read_text(errors="replace")
        except OSError as exc:
            raise LaunchError(f"{unit} left no result ({type(exc).__name__}): {done.stderr.strip()[-300:]}") from exc
        finally:
            (self.places.state / "public" / "launch" / f"{request_id}.json").unlink(missing_ok=True)
        for name in (f"{request_id}.exit", f"{request_id}.log"):
            (runs / name).unlink(missing_ok=True)
        if not status.isdigit():
            raise LaunchError(f"{unit} ended by {status or 'an unknown cause'}")
        return int(status), output

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        """Start a session and wait for `claude --bg` to hand it to the daemon; its request and output are removed
        then, so none is left behind for anyone to read."""
        code, output = self.start_waiting(request_id, launch)
        if code != 0:
            raise LaunchError(f"the session did not start (exit {code}): {output.strip()[-300:]}")

    def start_waiting(self, request_id: str, launch: dict[str, Any]) -> tuple[int, str]:
        """A session's start, waited on: how it exited and what `claude --bg` printed (the install's drill reads it)."""
        self._request(request_id, {"kind": "session", **launch})
        return self._wait(AGENT_UNIT.format(request_id), request_id)

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        request_id = f"cmd-{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "command", "argv": argv, "cwd": cwd})
        return self._wait(AGENT_UNIT.format(request_id), request_id)

    def stop_session(self, session_id: str) -> None:
        # `claude stop` refuses a full session id and takes its first 8 characters, the id `claude --bg` prints.
        code, output = self.agent_command(["claude", "stop", session_id[:8]], self.settings().get_str("repo.path"))
        if code != 0:
            raise LaunchError(f"claude stop {session_id} failed: {output.strip()[-300:]}")

    def message_session(self, session_id: str, text: str) -> None:
        """Hand the session ``text`` as its next user turn (runtime/system/runners/agent_run.py holds it out)."""
        request_id = f"msg-{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "message", "session": session_id, "text": text})
        code, output = self._wait(AGENT_UNIT.format(request_id), request_id)
        if code != 0:
            raise LaunchError(f"claude --resume exited {code}: {output.strip()[-300:]}")

    def run_check(self, checkout: str, args: list[str]) -> tuple[int, str]:
        """`factory check` with ``args`` on ``checkout``, in the check-run unit; its exit status and its report."""
        request_id = f"{CHECK_REQUEST}{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "check", "checkout": checkout, "args": args})
        return self._wait(CHECK_UNIT.format(request_id), request_id)

    def start_upgrade(self, commit: str) -> None:
        """Start the owner's upgrade to the release of ``commit``, as root, and return at once: the install it runs
        stops this warden."""
        done = self.run(["start", "--no-block", UPGRADE_UNIT.format(commit)])
        if done.returncode != 0:
            raise LaunchError(f"the upgrade did not start: {done.stderr.strip()[-300:]}")

    def restart_daemon(self) -> None:
        """Restart the daemon that hosts every session: it loads the sign-in token again, and its sessions end."""
        done = self.run(["restart", DAEMON_UNIT])
        if done.returncode != 0:
            raise LaunchError(f"the agents' Claude daemon did not restart: {done.stderr.strip()[-300:]}")

    def signed_in(self) -> str | None:
        """The token the agents' Claude daemon last signed in with: it writes its stored sign-in as it starts."""
        try:
            held = json.loads(stored_signin(agent_home()).read_text(encoding="utf-8"))
        # telemetry: expected — the daemon has not signed in yet: no token is held, `None`
        except FileNotFoundError:
            return None
        token = held.get("claudeAiOauth", {}).get("accessToken") if isinstance(held, dict) else None
        return token if isinstance(token, str) and token else None

    def answered(self, session_id: str, since: float) -> str | None:
        """How a session's first model call since ``since`` went (cost.call_outcome); None while it has made none."""
        found = sorted((agent_home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
        lines = found[0].read_text(encoding="utf-8", errors="replace").splitlines() if found else []
        return next((said for line in lines if (said := cost.call_outcome(line, since)) is not None), None)

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        request_id = f"stack-{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "stack", "slot": slot, "verb": verb, "session": session})
        code, output = self._wait(STACK_UNIT.format(request_id), request_id)
        log = self.places.evidence / "stack" / f"{request_id}.log"
        store.write_text(log, output, mode=0o600)
        settings = self.settings()
        lines = settings.get_int("slots.log_tail_lines")
        kept = output if verb[0] == leases.RUN else "\n".join(output.splitlines()[-lines:]) if lines else ""
        return StackResult(exit=code, tail=_last(kept, settings.get_int("slots.tail_max_kib") * 1024), log=str(log))


def _last(text: str, most: int) -> str:
    """The end of ``text`` in at most ``most`` UTF-8 bytes, cut at a character, saying how much went above it."""
    data = text.encode()
    if len(data) <= most:
        return text
    kept = data[-most:].decode(errors="ignore")
    return f"[{len(data) - len(kept.encode())} bytes above are cut]\n{kept}"


def definitions(ctx: Context, role: str, model: str, effort: str) -> dict[str, Any]:
    """The agent definitions a session of ``role`` runs under today; raises LaunchError when they cannot be read."""
    try:
        return session_settings.agents(ctx.settings(), ctx.places, role, model, effort)
    except (OSError, ValueError) as exc:
        raise LaunchError(f"the agent launchers cannot be read ({exc}); the factory needs a reinstall") from exc


def options(ctx: Context, request_id: str, s: Session, agents: dict[str, Any]) -> dict[str, str]:
    """What session ``s`` runs under: its model, ``agents``, and sandbox settings written now for ``request_id``."""
    path = session_settings.write(ctx.settings(), ctx.places, request_id, s.role, Path(s.worktree), agent_home())
    return {"model": s.model, "effort": s.effort, "agent": s.role, "settings": str(path), "agents": json.dumps(agents)}


def start_session(
    ctx: Context, sessions: dict[str, Session], fields: dict[str, Any], prompt: str, worktree: str
) -> Session:
    """Register and start one session named by ``fields``; its id stays pending until the backstop sees it run.

    It is registered before it starts, so the backstop knows the name it is about to see. A worktree the reap held for
    the task (work not yet on a merged branch) is handed to the new session.
    """
    settings = ctx.settings()
    held = reap.held_for(ctx.places, fields["task"]) if fields["task"] else None
    if held is not None:
        worktree = Path(held["worktree"]).name
        prompt += f"\n\nYour worktree holds the last session's work on {held['branch']}: carry on from it."
    request_id = f"{PENDING}{uuid.uuid4().hex}"
    repo = Path(settings.get_str("repo.path"))
    place = session_settings.worktrees(repo) / worktree if worktree else repo
    try:
        agents = definitions(ctx, fields["role"], fields["model"], fields["effort"])
    except LaunchError as exc:
        raise Refused("launch", str(exc)) from exc
    now = ctx.clock.iso()
    s = Session(id=request_id, parent="", worktree=str(place), state=WORKING, started=now, since=now, note="started",
                definitions=session_settings.fingerprint(agents), kinds=session_settings.kinds(agents, fields["role"]),
                **fields)  # fmt: skip
    carried = options(ctx, request_id, s, agents)
    sessions[request_id] = s
    registry.write(ctx.places, sessions)
    request = {"name": s.name, "cwd": str(repo), "worktree": worktree, "prompt": prompt,
               **carried}  # fmt: skip
    try:
        ctx.launcher.start_agent(request_id, request)
    except LaunchError as exc:
        del sessions[request_id]
        registry.write(ctx.places, sessions)
        raise Refused("launch", f"{s.name} did not start: {exc}") from exc
    if held is not None:
        reap.release_held(ctx.places, fields["task"])
    store.append_jsonl(ctx.places.history, {"at": now, "session": s.id, "name": s.name, "state": WORKING,
                                            "retired": "", "by": s.started_by, "why": "started"})  # fmt: skip
    return s
