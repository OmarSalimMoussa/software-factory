"""How the warden starts and resumes sessions, and the real Launcher under it.

The warden runs anything only by writing a request to public/launch/<id>.json and starting one of the two template
units polkit lets it start.

``software-agent-run@<id>.service`` runs as the agent user: a new session (``kind: session``) or one short command
(``kind: command``). ``software-stack-run@<id>.service`` runs ``scripts/dev`` as the stack user (``kind: stack``).
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
from runtime.shared.models import WORKING, Session
from runtime.shared.paths import AGENT_USER, Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.warden import reap, registry, session_settings
from runtime.warden.context import Context, StackResult

AGENT_UNIT = "software-agent-run@{}.service"
STACK_UNIT = "software-stack-run@{}.service"
# A started session's registry id until the backstop sees it running under its name and takes the id Claude Code
# gave it (`claude --bg` ignores --session-id). The guard finds an unbound session the same way, by name.
PENDING = "pending-"
REDACTED = "[redacted]"
# What `claude --bg` prints for the session it started. A resume that prints another short id started a copy.
_BACKGROUNDED = re.compile(r"^backgrounded · ([0-9a-f]{8})\b", re.MULTILINE)
# A value made only of digits or a boolean word carries no secret; redacting it would erase every port and flag.
_NOT_SECRET = re.compile(r"^(\d+|true|false|yes|no|on|off)$", re.IGNORECASE)

Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


class LaunchError(Exception):
    """A unit could not be started, or the command it ran failed; the message says which and why."""


def agent_home() -> Path:
    return Path(pwd.getpwnam(AGENT_USER).pw_dir)


def _carried(launch: dict[str, Any]) -> list[str]:
    """What a session runs under. A resume passes all of it again: Claude Code promises none of it carries over, and
    a session resumed without its --settings would run outside the sandbox."""
    return ["--remote-control", launch["name"], "--model", launch["model"], "--effort", launch["effort"],
            "--agent", launch["agent"], "--settings", launch["settings"], "--agents", launch["agents"]]  # fmt: skip


def session_argv(launch: dict[str, Any]) -> list[str]:
    """The `claude` command a session request names; the agent-run unit's runner executes exactly this."""
    argv = ["claude", "--bg", "--name", launch["name"], *_carried(launch)]
    if launch["worktree"]:
        argv += ["--worktree", launch["worktree"]]
    # Without the --, a prompt that starts with a dash would be read as a flag.
    return [*argv, "--", launch["prompt"]]


def _systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *argv], capture_output=True, text=True, check=False)


class Units:
    """Launcher through the two template units. ``run`` runs one systemctl command (a test passes a fake)."""

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
        self._request(request_id, {"kind": "session", **launch})
        done = self.run(["start", AGENT_UNIT.format(request_id)])
        if done.returncode != 0:
            raise LaunchError(f"the session did not start: {done.stderr.strip()[-300:]}")

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        request_id = f"cmd-{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "command", "argv": argv, "cwd": cwd})
        return self._wait(AGENT_UNIT.format(request_id), request_id)

    def stop_session(self, session_id: str) -> None:
        # `claude stop` refuses a full session id and takes its first 8 characters, the id `claude --bg` prints.
        code, output = self.agent_command(["claude", "stop", session_id[:8]], self.settings().get_str("repo.path"))
        if code != 0:
            raise LaunchError(f"claude stop {session_id} failed: {output.strip()[-300:]}")

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        request_id = f"stack-{uuid.uuid4().hex}"
        self._request(request_id, {"kind": "stack", "slot": slot, "verb": verb, "session": session})
        code, output = self._wait(STACK_UNIT.format(request_id), request_id)
        clean = redact(output, slot_secrets(self.places, slot))
        log = self.places.evidence / "stack" / f"{request_id}.log"
        store.write_text(log, clean, mode=0o600)
        lines = self.settings().get_int("slots.log_tail_lines")
        tail = "\n".join(clean.splitlines()[-lines:]) if lines else ""
        return StackResult(exit=code, tail=tail, log=str(log))


def slot_secrets(places: Places, slot: int) -> list[str]:
    """Every value in the slot's env file that could be a secret."""
    path = places.slot(slot) / "env"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LaunchError(f"{path} cannot be read to redact it: {type(exc).__name__}") from exc
    values = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        value = value.strip().strip("'\"")
        if sep and not key.lstrip().startswith("#") and value and not _NOT_SECRET.match(value):
            values.append(value)
    return values


def redact(text: str, secrets: list[str]) -> str:
    # Longest first, so a secret that contains another is replaced whole.
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def _options(ctx: Context, settings_id: str, role: str, model: str, effort: str, place: Path) -> dict[str, str]:
    """The sandbox settings and agent definitions a session runs under, rendered from the current settings."""
    settings = ctx.settings()
    path = session_settings.write(settings, ctx.places, settings_id, role, place, agent_home())
    try:
        agents = session_settings.agents(settings, ctx.places, role, model, effort)
    except (OSError, ValueError) as exc:
        raise Refused("launch", f"the agent launchers cannot be read ({exc}); the factory needs a reinstall") from exc
    return {"model": model, "effort": effort, "agent": role, "settings": str(path), "agents": json.dumps(agents)}


def resume(ctx: Context, s: Session, message: str) -> None:
    """Resume a stopped session under its own id with a message; raises LaunchError when it did not.

    A copy Claude Code starts instead (it does when it still counts the session as running) is removed at once.
    """
    if not message.strip():
        raise LaunchError("a resume needs a message")
    cwd = ctx.settings().get_str("repo.path")
    options = _options(ctx, s.id, s.role, s.model, s.effort, Path(s.worktree))
    argv = ["claude", "--resume", s.id, "--bg", *_carried({"name": s.name, **options}), "--", message]
    code, output = ctx.launcher.agent_command(argv, cwd)
    if code != 0:
        raise LaunchError(f"claude --resume failed: {output.strip()[-300:]}")
    started = _BACKGROUNDED.search(output)
    if started is None:
        raise LaunchError("claude --resume named no session it started")
    if not s.id.startswith(started.group(1)):
        ctx.launcher.agent_command(["claude", "rm", started.group(1)], cwd)
        raise LaunchError(f"Claude Code started a copy ({started.group(1)}) instead; it was removed, try again")


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
    options = _options(ctx, request_id, fields["role"], fields["model"], fields["effort"], place)
    now = ctx.clock.iso()
    s = Session(id=request_id, parent="", worktree=str(place), slot=None, state=WORKING, started=now, since=now,
                note="started", **fields)  # fmt: skip
    sessions[request_id] = s
    registry.write(ctx.places, sessions)
    request = {"name": s.name, "cwd": str(repo), "worktree": worktree, "prompt": prompt,
               **options}  # fmt: skip
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
