"""The rehearsal's world: the fixture product checked out in the agent's home, a bare repo standing in for GitHub,
and the warden wired as wardend wires it. Fakes stand only at the machine's edges: systemctl, the kernel's process
table with Claude Code's session records, the slot's running stack, the shared services' Docker, the health addresses
and GitHub's API.

systemctl is the one fake with logic: a session start passes the agent-run unit's own check of its request and is
recorded (no claude ever runs), and so are a `claude stop` and a message to a session, while a command or a stack
request runs the real unit runner, which runs git or the product's own scripts/dev. A check request passes the
check-run unit's own check of its request and passes: the checks themselves are tests/checks' to prove. Anything else
the warden asks a unit to run fails the rehearsal before it runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from settings_text import set_line

from runtime.cli import factory as cli
from runtime.qa import qa_browser
from runtime.shared import protocol, store
from runtime.shared.paths import SCRATCH, Places
from runtime.slots import health
from runtime.system.runners import launch_file
from runtime.warden import inbox, public, server, wardend
from runtime.warden.context import Caller, Clock, Context
from runtime.warden.delivery import github_push
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher, Doors, resolve
from runtime.warden.health.probes import Answer
from runtime.warden.owner import settings_save
from runtime.warden.sessions import backstop, crashwatch, launch, registry
from runtime.warden.verbs import VERBS
from standard import config

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "testdata" / "fixture-product"
RUNNERS = ROOT / "runtime" / "system" / "runners"
GITHUB = "acme/notes"
AGENT_UID, OWNER_UID, DASHBOARD_UID = 2001, 1000, 2002
T0 = datetime(2026, 10, 9, 9, 0, tzinfo=UTC).timestamp()
SNAPSHOT = "runtime.warden.snapshot:"
SERVICES_ENV = (
    "POSTGRES_PASSWORD=pg\nLANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-1\nLANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-1\n"
    "GLITCHTIP_OWNER_PASSWORD=gt\n"
)
# docker as a slot's provisioning drives it: networks are made and joined, psql takes its statements, and GlitchTip's
# shell answers with a DSN for each project it is asked for. Anything else fails, naming itself.
DOCKER = f"""#!{sys.executable}
import ast, json, re, sys
sys.path.insert(0, {str(ROOT)!r})
from standard.services import glitchtip_bootstrap as glitchtip
args = sys.argv[1:]
if args[:2] in (["network", "ls"], ["network", "create"], ["network", "connect"], ["network", "disconnect"],
                ["network", "rm"]):
    sys.exit(0)
if args[:2] == ["network", "inspect"]:
    sys.exit(print("{{}}"))
if args[:4] == ["exec", "-i", "factory-postgres", "psql"]:
    sys.stdin.read()
    sys.exit(0)
if args[:5] == ["exec", "-i", "factory-glitchtip", "./manage.py", "shell"]:
    params = json.loads(ast.literal_eval(re.search(r"^PARAMS = json.loads\\((.*)\\)$", sys.stdin.read(), re.M)[1]))
    dsns = {{name: f"http://key{{n}}@127.0.0.1:8200/{{n}}" for n, name in enumerate(params["projects"], 1)}}
    sys.exit(print(glitchtip.RESULT + json.dumps({{"dsns": dsns}})))
sys.exit(f"the rehearsal's docker does not do: {{' '.join(args)}}")
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeClock(Clock):
    """Each request to the warden moves time on, so every log line and record has its own time."""

    def __init__(self) -> None:
        self.t = T0

    def now(self) -> float:
        return self.t


class FakeGitHub:
    """GitHub's API answers only the control probe; the walk never promotes."""

    def dispatch_promote(self, sha: str, released: str) -> str:
        raise AssertionError("the walk never promotes")

    def ci_run(self, sha: str) -> str | None:
        return None  # CI has not started on it

    def full_sha(self, sha: str) -> str:
        return sha + "f" * (40 - len(sha))

    def run(self, run_id: str) -> dict[str, Any]:
        raise AssertionError("the walk never promotes")

    def reachable(self) -> bool:
        return True


def at_once(work: Callable[[], None]) -> None:
    """Settles the sessions pass's benches at once, so a test reads the result as the pass returns."""
    work()


@dataclass
class Systemd:
    """systemctl as launch.Units calls it; ``sessions`` holds every session start's launch request."""

    places: Places
    agent_home: Path
    bin: Path  # where the rehearsal's docker is, first on the units' PATH
    sessions: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)  # each `claude stop` the warden asked for, by short id
    messages: list[dict[str, Any]] = field(default_factory=list)  # each message handed to a running session

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        wait = argv[:1] == ["--wait"]
        verb, unit = argv[1:] if wait else argv
        assert verb == "start", argv
        request_id = unit.split("@", 1)[1].removesuffix(".service")
        raw = store.read_json(self.places.state / "public" / "launch" / f"{request_id}.json")
        assert wait, f"every unit is waited on: {argv}"
        runs = self.places.state / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        if raw["kind"] == "session":
            launch_file.session_argv(raw, self.places.state)
            self.sessions.append(raw)
            (runs / f"{request_id}.log").write_text("backgrounded · 0f00ba12\n")
            (runs / f"{request_id}.exit").write_text("0\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if raw["kind"] == "message":
            launch_file.message_argv(raw)
            self.messages.append(raw)
            (runs / f"{request_id}.log").write_text("")
            (runs / f"{request_id}.exit").write_text("0\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if raw["kind"] == "command" and raw["argv"][:2] == ["claude", "stop"]:
            launch_file.command_argv(raw, self.agent_home, self.places.state)
            self.stops.append(raw["argv"][2])
            (runs / f"{request_id}.log").write_text("")
            (runs / f"{request_id}.exit").write_text("0\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if raw["kind"] == "command":
            assert raw["argv"][0] == "git", f"the warden asked a unit to run {raw['argv']}"
        if raw["kind"] == "check":
            launch_file.check_parts(raw, self.agent_home)
            self.checks.append(raw)
            (runs / f"{request_id}.log").write_text("factory check: 28 passed\n")
            (runs / f"{request_id}.exit").write_text("0\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        runner = RUNNERS / ("agent_run.py" if unit.startswith("software-agent-run@") else "stack_run.py")
        # The unit runs as the agent (or stack) user, whose home holds the product checkout.
        env = {**os.environ, "HOME": str(self.agent_home), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        with (runs / f"{request_id}.log").open("w") as log:
            done = subprocess.run([sys.executable, "-I", str(runner), str(self.places.state), request_id],
                                  stdout=log, stderr=subprocess.STDOUT, env=env, timeout=60, check=False)  # fmt: skip
        (runs / f"{request_id}.exit").write_text(f"{done.returncode}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def started(self, name: str) -> dict[str, Any]:
        return next(raw for raw in self.sessions if raw["name"] == name)


@dataclass
class Kernel:
    """The process table the warden reads: /proc entries and Claude Code's record of each session process."""

    proc: Path
    records: Path
    next_pid: int = 4000

    def process(self, parent: int) -> int:
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        (self.proc / str(pid)).mkdir(parents=True)
        # Field 22 of stat, the start time, is the 20th after "pid (comm)".
        (self.proc / str(pid) / "stat").write_text(f"{pid} (claude) {' '.join(['S', *['0'] * 18, str(pid)])}\n")
        (self.proc / str(pid) / "status").write_text(f"Name:\tclaude\nPPid:\t{parent}\n")
        return pid

    def run_session(self, session_id: str, name: str) -> int:
        pid = self.process(parent=1)
        record = {"pid": pid, "sessionId": session_id, "procStart": str(pid), "name": name, "status": "busy",
                  "bridgeSessionId": f"session_{name}"}  # fmt: skip
        store.write_json(self.records / f"{pid}.json", record)
        return pid


@dataclass
class Stack:
    """What a slot's stack answers on its health address: the commit it was last brought up at."""

    serving: str = ""

    def http_get(self, url: str, timeout: int, limit: int) -> bytes:
        assert self.serving, f"{url} answered before any stack was up"
        return json.dumps({"build_commit": self.serving}).encode()


@dataclass
class Rehearsal:
    places: Places
    repo: Path
    github: Path
    clock: FakeClock
    systemd: Systemd
    kernel: Kernel
    stack: Stack
    dispatcher: Dispatcher
    peers: server.Peers
    pids: dict[str, int] = field(default_factory=dict)  # each running session's claude process
    answers: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _ask(self, caller: Caller, socket: str, line: bytes) -> dict[str, Any]:
        self.clock.t += 30
        answer = self.dispatcher.answer(caller, socket, line)
        self.answers.append((json.loads(line)["verb"], answer))
        assert answer["ok"], f"refused: {answer}"
        return answer

    def owner(self, argv: list[str]) -> dict[str, Any]:
        """The owner's own terminal: `factory <argv>` on the agent socket."""
        call = cli.build(argv, cli.read_file)
        caller = server.identify(self.places, self.peers, AGENT_SOCKET, pid=9000, uid=OWNER_UID)
        return self._ask(caller, AGENT_SOCKET, protocol.request(call.verb, call.args, call.data))

    def dashboard(self, verb: str, args: dict[str, Any]) -> dict[str, Any]:
        caller = server.identify(self.places, self.peers, OWNER_SOCKET, pid=9100, uid=DASHBOARD_UID)
        return self._ask(caller, OWNER_SOCKET, protocol.request(verb, args))

    def _from_session(self, session: str) -> Caller:
        """Who a process the session started is, as the socket server works it out from the process tree."""
        pid = self.kernel.process(parent=self.pids[session])
        return server.identify(self.places, self.peers, AGENT_SOCKET, pid=pid, uid=AGENT_UID)

    def session(self, session: str, argv: list[str], agent_id: str = "") -> dict[str, Any]:
        """`factory <argv>` run by the session, or by its subagent ``agent_id``, whose id the guard puts first."""
        call = cli.build(["--agent-id", agent_id, *argv] if agent_id else argv, cli.read_file)
        line = protocol.request(call.verb, call.args, call.data, agent_id=call.agent_id)
        return self._ask(self._from_session(session), AGENT_SOCKET, line)

    def hook(self, session: str, verb: str, args: dict[str, Any]) -> dict[str, Any]:
        """The guard's SubagentStart or SubagentStop hook, which runs in the session's own process tree."""
        return self._ask(self._from_session(session), AGENT_SOCKET, protocol.request(verb, args))

    def warden(self) -> Context:
        return self.dispatcher.doors.context(wardend.WARDEN)

    def body(self, folder: Path, name: str, data: bytes) -> str:
        """A body a caller hands the factory command: a file under its checkout's .factory/, which git ignores."""
        path = folder / SCRATCH / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def run_started(self, name: str) -> str:
        """The session the warden started as ``name`` comes up, its worktree made as `claude --worktree` makes it;
        then one session watch binds it. Its own id."""
        raw = self.systemd.started(name)
        if raw["worktree"]:
            where = self.repo / ".claude" / "worktrees" / raw["worktree"]
            git(self.repo, "worktree", "add", "-q", "-b", f"worktree-{raw['worktree']}", str(where), "master")
        session_id = str(uuid.uuid4())  # `claude --bg` picks the id itself
        self.pids[session_id] = self.kernel.run_session(session_id, name)
        crashwatch.SessionsWatch(start=at_once).run_pass(self.warden())
        return session_id

    def health(self, staging: str, prod: str) -> Any:
        """Staging and prod, answering on the health addresses the product's factory.toml names."""
        product = config.load(self.repo)
        addresses = {f"https://{product.workspace(env).domain}{product.deploy.health}": commit
                     for env, commit in (("staging", staging), ("prod", prod))}  # fmt: skip

        def fetch(url: str, timeout: float) -> Answer:
            return Answer(200, json.dumps({"build_commit": addresses[url]}).encode())

        return fetch


def _settings_text(repo: Path) -> str:
    text = (ROOT / "settings.toml").read_text()
    for key, value in {"repo.path": str(repo), "repo.github": GITHUB}.items():
        text = set_line(text, key, value)
    return text


def _product(tmp: Path, repo: Path, places: Places, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The agent's checkout of the fixture product, and the bare repo its origin reaches."""
    github = tmp / "github" / "notes.git"
    config = tmp / "gitconfig"
    config.write_text(
        f'[url "{github}"]\n\tinsteadOf = git@github.com:{GITHUB}.git\n'
        "[user]\n\tname = Rehearsal\n\temail = rehearsal@example.invalid\n[init]\n\tdefaultBranch = master\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    github.parent.mkdir(parents=True)
    git(github.parent, "init", "-q", "--bare", str(github))
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__", ".*_cache", "queue"))
    git(repo, "init", "-q", "-b", "master")
    # The install points the checkout's hooks at its own folder; this install has none, so no hook runs.
    git(repo, "config", "core.hooksPath", str(places.install / "checks" / "git-hooks"))
    (repo / ".git" / "info" / "exclude").write_text(f"/{SCRATCH}/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "The notes app")
    git(repo, "remote", "add", "origin", f"git@github.com:{GITHUB}.git")
    git(repo, "push", "-q", "origin", "master")
    (repo / "queue").mkdir(mode=0o755)
    (repo / "queue").chmod(0o755)  # the warden refuses a queue others may write
    return github


def build(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Rehearsal:
    """The rehearsal set up as the installer and wardend's start leave the machine."""
    places = Places.under(tmp / "factory")
    agent_home = tmp / "agent"
    repo = agent_home / "notes"
    github = _product(tmp, repo, places, monkeypatch)
    places.install.mkdir(parents=True)
    places.defaults.write_text(_settings_text(repo))
    for part in ("plugin", "procedures", "rules"):
        shutil.copytree(ROOT / part, places.install / part)
    settings = wardend.FreshSettings(places)
    clock = FakeClock()
    public.write_settings(places, settings())
    public.write_switches(places, public.read_switches(places))
    registry.write(places, registry.read(places))
    settings_save.render_plugin(places, settings())
    store.write_json(places.vitals, {"at": clock.iso(), "level": "green", "readings": []})
    key = github_push.deploy_key(places)
    key.parent.mkdir(parents=True)
    key.write_text("not a real key\n")

    kernel = Kernel(tmp / "proc", agent_home / ".claude" / "sessions")
    kernel.records.mkdir(parents=True)
    stack = Stack()
    monkeypatch.setattr(launch, "agent_home", lambda: agent_home)
    monkeypatch.setattr(backstop, "PROC", kernel.proc)
    daemon = clock.now() - 86400  # the agent's Claude daemon has run since the day before
    monkeypatch.setattr(backstop, "daemon_started", lambda: daemon)
    monkeypatch.setattr(qa_browser, "runs_root", lambda: agent_home / qa_browser.RUNS)
    monkeypatch.setattr(health, "http_get", stack.http_get)

    places.services.mkdir(parents=True)
    (places.services / "services.env").write_text(SERVICES_ENV)
    (tmp / "bin").mkdir()
    (tmp / "bin" / "docker").write_text(DOCKER)
    (tmp / "bin" / "docker").chmod(0o755)
    systemd = Systemd(places, agent_home, tmp / "bin")
    doors = Doors(places, settings, clock, launch.Units(places, settings, run=systemd), FakeGitHub(),
                  inbox.poster(places, clock), threading.RLock())  # fmt: skip
    # The dashboard's snapshot is being rebuilt beside this rehearsal; every other verb resolves as wardend's do.
    handlers = resolve({name: verb for name, verb in VERBS.items() if not verb.handler.startswith(SNAPSHOT)})
    peers = server.Peers(AGENT_UID, OWNER_UID, DASHBOARD_UID, kernel.records, kernel.proc)
    return Rehearsal(places, repo, github, clock, systemd, kernel, stack, Dispatcher(doors, handlers), peers)
