from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.models import RETIRED, WAITING
from runtime.shared.protocol import Refused
from runtime.warden.sessions import (
    backstop,
    doing,
    launch,
    lifecycle,
    older_skills,
    registry,
    respawn,
    session_settings,
)
from runtime.warden.sessions.backstop import Proc
from runtime.warden.sessions.launch import LaunchError

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"


@pytest.mark.parametrize("role", ["orchestrator", "implementer"])
def test_each_session_runs_unsandboxed_with_every_tool_allowed_and_no_auto_memory(world: World, role: str) -> None:
    body = session_settings.render(world.places, role)
    assert body["sandbox"] == {"enabled": False}
    assert body["permissions"] == {"defaultMode": "bypassPermissions"}
    assert body["autoMemoryEnabled"] is False
    assert body["worktree"] == {"bgIsolation": "none"}
    assert ("statusLine" in body) == (role == "orchestrator")


def test_a_session_gets_its_role_agents_with_their_tool_servers(world: World) -> None:
    world.approve()
    lifecycle.start(world.ctx(), {"role": "implementer", "task": "t-0001"}, None)
    request = world.launcher.started[0][1]
    agents = json.loads(request["agents"])
    servers = {
        name: [s if isinstance(s, str) else next(iter(s.values()))["args"] for s in a.get("mcpServers", [])]
        for name, a in agents.items()
    }
    # The UI testers drive their own browser, each lane its own: two definitions alike would share one browser. The
    # implementer reaches the slot's API, and its API testers through its own server, for the same reason.
    lanes = ["", *(f"-{n}" for n in range(2, world.settings.get_int("caps.browsers_at_once") + 1))]
    assert servers == {
        "implementer": [["slot-api"]],
        "qa-backend": ["slot-api"],
        "qa-security": ["slot-api"],
        **{f"{kind}{lane}": [["qa-browser", f"{kind}{lane}"]] for kind in ("qa-ui-function", "qa-ux", "qa-visual")
           for lane in lanes},
    }  # fmt: skip
    assert (agents["implementer"]["model"], agents["implementer"]["effort"]) == (request["model"], request["effort"])
    assert launch.session_argv(request)[-2:] == [
        "--",
        "Task: t-0001, Task t-0001\n\n`factory queue task show t-0001` gives its log, its requests and its spec."
        "\n\nSlot 1 is yours: bring it up with factory slot up.",
    ]


def test_a_stack_run_keeps_what_the_runner_printed_and_hands_back_its_tail(world: World) -> None:
    out = "\n".join(["starting", *[f"line {n}" for n in range(50)], "http://127.0.0.1:8102/health"])

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", out)
        store.write_text(world.places.state / "runs" / f"{request}.exit", "3\n")
        return subprocess.CompletedProcess(argv, 1, "", "")

    result = launch.Units(world.places, lambda: world.settings, systemctl).run_stack(2, ["up"], "5e550001")
    tail = world.settings.get_int("slots.log_tail_lines")
    assert result.exit == 3 and len(result.tail.splitlines()) == tail
    assert Path(result.log).read_text() == out and result.tail.endswith("http://127.0.0.1:8102/health")
    assert not list((world.places.state / "runs").iterdir())


def test_a_stack_runs_input_waits_where_only_the_warden_reads_and_goes_after(world: World) -> None:
    held: list[tuple[str, int, str]] = []
    runs = world.places.state / "runs"

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        given = runs / f"{request}.in"
        public = (world.places.state / "public" / "launch" / f"{request}.json").read_text()
        held.append((given.read_text(), given.stat().st_mode & 0o777, public))
        store.write_text(runs / f"{request}.log", "")
        store.write_text(runs / f"{request}.exit", "0\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    launch.Units(world.places, lambda: world.settings, systemctl).run_stack(0, ["reports"], "", '{"words": "hi"}')
    [(given, mode, public)] = held
    assert (given, mode) == ('{"words": "hi"}', 0o600) and "hi" not in public
    assert not list(runs.iterdir())


@pytest.mark.parametrize(
    ("verb", "out", "tail"),
    [
        (["up"], "start\nline 1\n" + "é" * 1500 + "!", "[1991 bytes above are cut]\n" + "é" * 511 + "!"),
        (["run", "web", "--", "ls"], "\n".join(f"line {n}" for n in range(9)), None),
    ],
    ids=["one-long-line-cut-at-a-character", "run-whole"],
)
def test_a_stack_answer_holds_at_most_tail_max_kib_and_a_run_all_of_its_output(
    world: World, verb: list[str], out: str, tail: str | None
) -> None:
    world.settings = world.settings.with_changes({"slots.log_tail_lines": 3, "slots.tail_max_kib": 1})

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", out)
        store.write_text(world.places.state / "runs" / f"{request}.exit", "0\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = launch.Units(world.places, lambda: world.settings, systemctl).run_stack(2, verb, "5e550001")

    assert result.tail == (out if tail is None else tail)


def test_a_resume_respawns_the_session_with_its_start_options_and_then_hands_it_the_message(world: World) -> None:
    s = world.seat("1234abcd-0000", worktree=world.settings.get_str("repo.path"))
    respawn.resume(world.ctx(), s, "carry on")
    assert world.launcher.commands == [["claude", "respawn", "1234abcd"]]
    written = session_settings.folder(world.places) / "pending-1234abcd-0000.json"
    assert json.loads(written.read_text())["permissions"] == {"defaultMode": "bypassPermissions"}
    assert world.launcher.messages == [("1234abcd-0000", "carry on")]


@pytest.mark.parametrize(
    ("trouble", "said"),
    [("lost", "claude respawn failed"), ("stays_down", "did not run again"), ("unbound", "not in the history")],
)
def test_a_resume_that_cannot_bring_the_session_back_fails_and_sends_nothing(
    world: World, trouble: str, said: str
) -> None:
    s = world.seat("1234abcd-0000")
    if trouble == "unbound":
        world.places.history.unlink()
    else:
        setattr(world.launcher, trouble, True)
    with pytest.raises(LaunchError, match=said):
        respawn.resume(world.ctx(), s, "carry on")
    assert world.launcher.messages == []


@pytest.mark.parametrize("place", ["", "impl-t-0001"], ids=["main-checkout", "worktree"])
def test_a_resume_with_older_agent_definitions_comes_back_as_a_copy_with_todays_under_the_copys_id(
    world: World, place: str
) -> None:
    repo = Path(world.settings.get_str("repo.path"))
    s = world.seat("1234abcd-0000", definitions="older", state=WAITING,
                   worktree=str(repo / ".claude" / "worktrees" / place if place else repo))  # fmt: skip
    store.write_json(world.places.ready(s.id), {"records": [{"head": "abc"}]})

    back = respawn.resume(world.ctx(), s, "carry on")

    [(request_id, request)] = world.launcher.started
    assert launch.session_argv(request)[:4] == ["claude", "--bg", "--resume", s.id]
    assert (request["worktree"], request["prompt"], world.launcher.messages) == (place, "carry on", [])
    agents = launch.definitions(world.ctx(), s.role, s.model, s.effort)
    assert json.loads(request["agents"]) == agents
    row = world.session(back)
    assert (back, row.name, row.state, row.definitions) == (f"copy-{s.id}", s.name, WAITING,
                                                            session_settings.fingerprint(agents))  # fmt: skip
    assert s.id not in registry.read(world.places)
    assert store.read_json(world.places.ready(back)) == {"records": [{"head": "abc"}]}
    # Its next revive respawns the copy, under the settings file its copy's start wrote.
    respawn.resume(world.ctx(), row, "carry on again")
    assert world.launcher.commands == [["claude", "respawn", back[:8]]]
    assert (session_settings.folder(world.places) / f"{request_id}.json").exists()


def test_a_copy_that_never_runs_leaves_the_session_as_it_was_for_its_next_revive(world: World) -> None:
    s = world.seat("1234abcd-0000", definitions="older")
    world.launcher.stays_down = True

    with pytest.raises(LaunchError, match="did not run within"):
        respawn.resume(world.ctx(), s, "carry on")

    assert registry.read(world.places) == {s.id: s}


@pytest.mark.parametrize("trouble", ["start", "move"], ids=["its-start-fails", "a-move-after-it-runs-fails"])
def test_a_renew_that_fails_any_way_leaves_the_session_as_it_was(
    world: World, monkeypatch: pytest.MonkeyPatch, trouble: str
) -> None:
    s = world.seat("1234abcd-0000", definitions="older", state=WAITING)
    store.write_json(world.places.ready(s.id), {"records": [{"head": "abc"}]})
    failed: list[str] = []

    def fail(*args: object) -> None:
        if not failed:
            failed.append(trouble)
            raise OSError("No space left on device")

    if trouble == "start":
        monkeypatch.setattr(world.launcher, "start_agent", fail)
    else:
        monkeypatch.setattr(older_skills, "renamed", fail)

    with pytest.raises(OSError, match="No space"):
        respawn.resume(world.ctx(), s, "carry on")

    assert registry.read(world.places) == {s.id: s}
    assert store.read_json(world.places.ready(s.id)) == {"records": [{"head": "abc"}]}


@pytest.mark.parametrize("when", ["before-its-copy-runs", "once-its-copy-runs"])
def test_an_owner_stop_while_a_session_comes_back_as_a_copy_stays(
    world: World, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    s = world.seat("1234abcd-0000", definitions="older")
    world.launcher.stays_down = True
    sleep = world.clock.sleep

    def owner_stops(seconds: float) -> None:
        if not world.launcher.stopped:
            if when == "once-its-copy-runs":
                world.procs["c0b1e5ed"] = Proc(pid=7000, session="c0b1e5ed", name=s.name, chat_link="")
                backstop.sweep(world.ctx(), dict(world.procs))
            [held] = registry.read(world.places)
            lifecycle.owner_stop(world.owner(), {"session": held}, None)
        sleep(seconds)

    monkeypatch.setattr(world.clock, "sleep", owner_stops)
    try:
        respawn.resume(world.ctx(), s, "carry on")
    except LaunchError as exc:
        assert "stopped" in str(exc)

    assert [row.state for row in registry.read(world.places).values()] == [RETIRED]


def test_all_a_session_held_under_its_id_follows_its_copy_and_its_old_id_finds_it(world: World) -> None:
    s = world.seat("1234abcd-0000", definitions="older")
    world.seat("agent-qa1", role="qa-backend", parent=s.id, agent_id="agent-qa1", state=RETIRED)
    (world.places.install / "COMMIT").write_text("abc1234\n")
    older_skills.note_install(world.places)  # it holds an older factory's skills, still to be told
    for path in world.places.session_files(s.id):
        store.write_text(path, f"{path.name}\n")

    back = respawn.resume(world.ctx(), s, "carry on")

    for old, new in zip(world.places.session_files(s.id), world.places.session_files(back), strict=True):
        assert (old.exists(), new.read_text()) == (False, f"{old.name}\n")
    assert world.session("agent-qa1").parent == back and older_skills.holds(world.places, back)
    assert lifecycle.find(world.ctx(), registry.read(world.places), s.id).id == back


@pytest.mark.parametrize("back", [False, True])
def test_a_deploy_names_the_full_commit_and_a_push_back_and_hands_back_how_it_ended(world: World, back: bool) -> None:
    sha, asked = "a" * 40, []

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        unit, launched = argv[-1], world.places.state / "public" / "launch"
        request = unit.split("@", 1)[1].removesuffix(".service")
        asked.append((argv[:2], unit.split("@")[0], json.loads((launched / f"{request}.json").read_text())))
        store.write_text(world.places.state / "runs" / f"{request}.log", "build app: step 1\nfailed: no space\n")
        store.write_text(world.places.state / "runs" / f"{request}.exit", "1\n")
        return subprocess.CompletedProcess(argv, 1, "", "")

    done = launch.Units(world.places, lambda: world.settings, systemctl).deploy_prod(sha, back)

    assert asked == [(["--wait", "start"], "software-deploy-run", {"kind": "deploy", "sha": sha, "back": back})]
    assert done == (1, "build app: step 1\nfailed: no space\n")
    assert not list((world.places.state / "runs").iterdir())


@pytest.mark.parametrize("text", ["x" * 61, "<b>bold</b>", "a & b", "\x1b[31mred"])
def test_a_session_own_doing_line_stays_short_and_plain(text: str) -> None:
    with pytest.raises(Refused):
        doing.clean(text)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_session_start_is_waited_on_and_leaves_no_request_or_output_behind(world: World, exit_code: int) -> None:
    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "--wait"
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", "backgrounded · 0f00ba12\n")
        store.write_text(world.places.state / "runs" / f"{request}.exit", f"{exit_code}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    units = launch.Units(world.places, lambda: world.settings, systemctl)
    if exit_code:
        with pytest.raises(launch.LaunchError, match="did not start"):
            units.start_agent("pending-1", {"name": "impl-t-0001"})
    else:
        units.start_agent("pending-1", {"name": "impl-t-0001"})

    assert not list((world.places.state / "public" / "launch").iterdir())
    assert not list((world.places.state / "runs").iterdir())


def test_a_starting_warden_drops_every_check_and_stack_request_a_warden_left(world: World) -> None:
    launched, runs = world.places.state / "public" / "launch", world.places.state / "runs"
    left = [launched / "check-1.json", runs / "check-1.log", runs / "check-1.exit", launched / "pending-1.json"]
    left += [launched / "stack-1.json", runs / "stack-1.in", runs / "stack-1.log", runs / "stack-1.exit"]
    for path in left:
        store.write_text(path, "{}\n")

    launch.forget_requests(world.places)

    assert [path for path in left if path.exists()] == [launched / "pending-1.json"]
