from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from system_fakes import FIXTURE, settings

from runtime.shared.paths import SLOT_KEYS, Places
from runtime.system.runners import docker_reach, same_up, stack_run
from runtime.system.runners import launch_file as lf
from standard import envfile
from standard.services import provision

SLOT = 2
REQUEST = "stack-7f3a"
# scripts/dev as a stand-in: it records what it was told and whether the env file was there, keeps its seeds beside
# the env file, then exits as asked.
DEV = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
env_file = Path(os.environ["FACTORY_ENV_FILE"])
Path(__file__).with_name("ran.json").write_text(json.dumps(
    {{"verb": sys.argv[1:], "env_file": str(env_file), "written": env_file.is_file()}}))
(env_file.parent / "seeds").mkdir(exist_ok=True)
(env_file.parent / "seeds" / "users.json").write_text("[]")
sys.exit(int(os.environ["DEV_EXIT"]))
"""


def _never(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the fakes run nothing, yet {argv} ran")


class Shared:
    """provision.workspace and provision.drop, recording each call."""

    def __init__(self, checkout: Path, fail: bool = False) -> None:
        self.checkout, self.fail = checkout, fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def workspace(self, config: object, run: object, **kw: Any) -> dict[str, str]:
        if self.fail:
            raise provision.ProvisionError("GlitchTip (factory-glitchtip) refused: no such organization")
        self.calls.append(("workspace", kw))
        return {"FACTORY_SERVICES_NETWORK": "notes-slot2-services", "DATABASE_URL": "postgresql://n:pw@db:5432/n"}

    def drop(self, config: object, run: object, **kw: Any) -> None:
        assert (self.checkout / "scripts" / "ran.json").is_file(), "dropped before scripts/dev ran"
        self.calls.append(("drop", kw))


# docker as a stand-in: logs each call and answers a listing; slot 2's network still holds two unlabelled containers.
DOCKER = f"""#!{sys.executable}
import os, sys, time
with open(os.environ["DOCKER_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
answers = {{"container ls": "c-web", "network ls": "n-slot2", "network inspect": "agent-7 agent-8 ",
           "container inspect": '["c-web", "/notes-slot2-web", false]'}}
print(answers.get(" ".join(sys.argv[1:3]), ""))
time.sleep(float(sys.argv[-1]) if sys.argv[-2:-1] == ["sleep"] else 0)
"""


@pytest.fixture(autouse=True)
def docker_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every docker call a test makes reaches the stand-in, never this machine's Docker."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").write_text(DOCKER)
    (tmp_path / "bin" / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("DOCKER_LOG", str(tmp_path / "docker.log"))
    return tmp_path / "docker.log"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    for name in ("factory.toml", "notes.env.template"):
        shutil.copy(FIXTURE / name, root / name)
    (root / "scripts" / "dev").write_text(DEV)
    (root / "scripts" / "dev").chmod(0o755)
    places = Places.under(tmp_path)
    places.keys.mkdir(parents=True)
    return root


def _verb(tmp_path: Path, checkout: Path, shared: Shared, monkeypatch: pytest.MonkeyPatch, verb: str) -> int:
    monkeypatch.setattr(provision, "workspace", shared.workspace)
    monkeypatch.setattr(provision, "drop", shared.drop)
    return stack_run.product_verb(
        Places.under(tmp_path), settings(), SLOT, [verb], checkout, run=_never, timeout=60, request=REQUEST
    )


@pytest.mark.parametrize(
    ("verb", "dev_exit", "dropped"),
    [("up", 0, False), ("empty", 0, True), ("empty", 3, False)],
    ids=["up", "empty", "empty-failed"],
)
def test_a_verb_runs_on_a_provisioned_slot_and_a_successful_empty_drops_it(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, verb: str, dev_exit: int, dropped: bool
) -> None:
    monkeypatch.setenv("DEV_EXIT", str(dev_exit))
    shared = Shared(checkout)
    places = Places.under(tmp_path)
    env_file = places.slot_env_dir(SLOT) / "notes.env"

    status = _verb(tmp_path, checkout, shared, monkeypatch, verb)

    ran = json.loads((checkout / "scripts" / "ran.json").read_text())
    assert status == dev_exit
    assert ran == {"verb": [verb], "env_file": str(env_file), "written": True}
    assert shared.calls[0] == ("workspace", {"workspace": "slot2", "origin": "http://127.0.0.1:8102",
                                             "commit": "dev-slot2", "state": places.slot_state_dir(SLOT)})  # fmt: skip
    assert [name for name, _ in shared.calls[1:]] == (["drop"] if dropped else [])
    assert env_file.exists() is not dropped
    assert envfile.defaults_path(env_file).exists() is not dropped  # nothing of the slot's file stays behind
    # The factory removes only what it wrote; the product's own files beside it are its scripts' to clear.
    assert (env_file.parent / "seeds" / "users.json").exists()


@pytest.mark.parametrize(
    ("slot", "zero_origin", "origin"),
    [
        (0, "http://203.0.113.7:8090", "http://203.0.113.7:8090"),
        (2, "http://203.0.113.7:8090", "http://127.0.0.1:8102"),
        (0, "", "http://127.0.0.1:8100"),
    ],
    ids=["staging", "another-slot", "no-address"],
)
def test_staging_tells_its_product_the_boxs_address_and_every_other_slot_its_loopback(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, slot: int, zero_origin: str, origin: str
) -> None:
    monkeypatch.setenv("DEV_EXIT", "0")
    shared = Shared(checkout)
    monkeypatch.setattr(provision, "workspace", shared.workspace)
    places = Places.under(tmp_path)

    stack_run.product_verb(places, settings(**{"slots.zero_origin": zero_origin}), slot, ["up"], checkout,
                           run=_never, timeout=60, request=REQUEST)  # fmt: skip

    assert shared.calls[0][1]["origin"] == origin
    assert f"NOTES_ORIGIN={origin}\n" in (places.slot_env_dir(slot) / "notes.env").read_text()


def test_a_slot_that_cannot_be_provisioned_runs_no_verb(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEV_EXIT", "0")

    status = _verb(tmp_path, checkout, Shared(checkout, fail=True), monkeypatch, "up")

    assert status == 1
    assert not (checkout / "scripts" / "ran.json").exists()
    assert "slot 2 could not be provisioned, so up did not run: GlitchTip" in capsys.readouterr().out


FACTORY_URL = "postgresql://n:pw@db:5432/n"


@pytest.mark.parametrize(
    ("copied", "value", "said"),
    [
        ("NOTES_SYNC_KEY=sk-real\n", "sk-real", None),
        (None, "REPLACE_ME", "slot 2 runs with a placeholder in NOTES_SYNC_KEY, so a call that needs one fails"),
        (
            "DATABASE_URL=postgresql://mine\n",
            "REPLACE_ME",
            "slots.keys names DATABASE_URL, which the factory sets for each slot: the slot keeps its own",
        ),
    ],
    ids=["copied", "none-copied", "factory-key"],
)
def test_a_slot_takes_the_owners_copied_keys_and_names_a_placeholder_left(
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    copied: str | None,
    value: str,
    said: str | None,
) -> None:
    monkeypatch.setenv("DEV_EXIT", "0")
    places = Places.under(tmp_path)
    with (checkout / "notes.env.template").open("a") as template:
        template.write("NOTES_SYNC_KEY=REPLACE_ME\n")
    if copied is not None:
        (places.keys / SLOT_KEYS).write_text(copied)

    assert _verb(tmp_path, checkout, Shared(checkout), monkeypatch, "up") == 0

    written = envfile.parse_values((places.slot_env_dir(SLOT) / "notes.env").read_text(), "notes.env")[0]
    out = capsys.readouterr().out
    assert (written["NOTES_SYNC_KEY"], written["DATABASE_URL"]) == (value, FACTORY_URL)
    assert (said in out) if said else "placeholder" not in out


def test_a_key_the_owner_withdraws_leaves_the_slot_at_its_next_command(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEV_EXIT", "0")
    places = Places.under(tmp_path)
    with (checkout / "notes.env.template").open("a") as template:
        template.write("NOTES_SYNC_KEY=REPLACE_ME\n")
    (places.keys / SLOT_KEYS).write_text("NOTES_SYNC_KEY=sk-real\n")
    _verb(tmp_path, checkout, Shared(checkout), monkeypatch, "up")
    (places.keys / SLOT_KEYS).unlink()

    _verb(tmp_path, checkout, Shared(checkout), monkeypatch, "up")

    # A value no one supplies is kept as set by hand: a withdrawn key stayed in every slot, and nothing said so.
    written = envfile.parse_values((places.slot_env_dir(SLOT) / "notes.env").read_text(), "notes.env")[0]
    assert written["NOTES_SYNC_KEY"] == "REPLACE_ME"
    assert "placeholder in NOTES_SYNC_KEY" in capsys.readouterr().out


def test_the_sweep_removes_the_containers_still_on_a_slots_network_before_the_network(
    docker_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = stack_run.sweep(SLOT, 60)

    removals = [line for line in docker_log.read_text().splitlines() if " rm " in f" {line} "]
    assert status == 0
    assert removals == ["container rm -f c-web", "container rm -f agent-7 agent-8", "network rm n-slot2"]
    assert "removed 2 container(s) still on slot 2's network n-slot2: agent-7, agent-8" in capsys.readouterr().out


def _inspected(cid: str, name: str, restarts: int, labels: dict[str, str] | None) -> str:
    return json.dumps({"id": cid, "name": f"/{name}", "restarts": restarts, "labels": labels})


def test_the_restarts_verb_prints_each_containers_count_as_one_line_but_a_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = stack_run.COMPOSE_PROJECT
    inspected = [
        _inspected("0123456789abcdef", "notes-slot2-app", 3, {stack_run.SLOT_LABEL: "2", project: "notes-slot2"}),
        _inspected("fedcba9876543210", "notes-slot2-run-1", 1, {stack_run.SLOT_LABEL: "2", stack_run.RUN_LABEL: "r-1"}),
        _inspected("00aa11bb22cc33dd", "notes-prod-app", 0, {project: "notes-prod"}),
        _inspected("99ee88ff77aa66bb", "loose", 0, None),
    ]
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        if args[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(args, 0, "0123456789abcdef\nfedcba9876543210\ngone\n", "")
        # A container removed between the list and the inspect is gone, which fails nothing.
        return subprocess.CompletedProcess(args, 1, "\n".join(inspected) + "\n", "Error: No such container: gone\n")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.restarts(30) == 0

    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert printed == [
        {"id": "0123456789ab", "name": "notes-slot2-app", "restarts": 3, "slot": "2", "project": "notes-slot2"},
        {"id": "00aa11bb22cc", "name": "notes-prod-app", "restarts": 0, "slot": "", "project": "notes-prod"},
        {"id": "99ee88ff77aa", "name": "loose", "restarts": 0, "slot": "", "project": ""},
    ]
    assert asked[1][:3] == ["container", "inspect", "--format"]
    assert asked[1][4:] == ["0123456789abcdef", "fedcba9876543210", "gone"]


def test_the_restarts_verb_fails_when_docker_cannot_say(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(args, 0, "0123456789abcdef\n", "")
        return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon\n")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.restarts(30) == 1
    assert "restarts: inspecting the containers failed: Cannot connect to the Docker daemon" in capsys.readouterr().out


def test_top_prints_each_container_running_for_the_slot_with_its_processes_cpu_and_names_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[list[str]] = []
    answers = {
        "container ls": "0123456789ab\n",
        "network ls": "n-slot2\n",
        "network inspect": "fedcba9876543210fedcba98 \n",  # a visit the product started, with no slot label
        "container stats": "0123456789ab notes-slot2-web 2.10% CPU, 90MiB / 1GiB\n"
        "fedcba987654 notes-signin-1 150.00% CPU, 600MiB / 2GiB\n",
        "container top": "PID       %CPU      ELAPSED   COMMAND\n7         140       01:02     neko\n",
    }

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        return subprocess.CompletedProcess(args, 0, answers[" ".join(args[:2])], "")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.top(SLOT, 30) == 0

    printed = capsys.readouterr().out
    assert "notes-slot2-web 2.10% CPU" in printed and "notes-signin-1 150.00% CPU" in printed
    assert "  7 140 01:02 neko" in printed
    stats = next(a for a in asked if a[:2] == ["container", "stats"])
    assert stats[-2:] == ["0123456789ab", "fedcba987654"]
    assert all(a[-1] == "pid,pcpu,etime,comm" for a in asked if a[:2] == ["container", "top"])


def test_top_fails_when_docker_cannot_list_the_slots_containers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon\n")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.top(SLOT, 30) == 1
    assert "top: listing slot 2's containers failed: Cannot connect" in capsys.readouterr().out


SLOT_IMAGES = ["image", "prune", "-a", "-f", "--filter", "label=software.slot"]


@pytest.mark.parametrize(
    ("ages", "status", "expected"),
    [
        (
            ["168", "48"],
            0,
            [["builder", "prune", "--all", "-f", "--filter", "until=48h"], [*SLOT_IMAGES, "--filter", "until=168h"]],
        ),
        (["0", "0"], 0, [["builder", "prune", "--all", "-f"], SLOT_IMAGES]),
        (["7d", "48"], lf.REFUSED, []),
    ],
)
def test_the_prune_removes_build_cache_and_only_slot_images_past_their_ages_since_prods_share_the_docker(
    monkeypatch: pytest.MonkeyPatch, ages: list[str], status: int, expected: list[list[str]]
) -> None:
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.prune(*ages, 30) == status
    assert asked == expected


# scripts/dev listing the verbs in verbs.txt beside it; its run records its label, then sleeps the seconds it ends with.
RUN_DEV = f"""#!{sys.executable}
import os, sys, time
from pathlib import Path
if sys.argv[1] == "verbs":
    sys.exit(print(Path(__file__).with_name("verbs.txt").read_text()))
Path(__file__).with_name("ran.json").write_text(os.environ["FACTORY_RUN_LABEL"])
time.sleep(float(sys.argv[-1]))
"""


@pytest.mark.parametrize(
    ("verbs", "status", "ran"), [("up run verbs", lf.REFUSED, None), ("up restart verbs", 0, "restart web")]
)
def test_a_restart_needs_the_products_restart_verb(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, verbs: str, status: int, ran: str | None
) -> None:
    dev = RUN_DEV.replace('os.environ["FACTORY_RUN_LABEL"]', '" ".join(sys.argv[1:])')
    dev = dev.replace("float(sys.argv[-1])", "0")
    (checkout / "scripts" / "dev").write_text(dev)
    (checkout / "scripts" / "verbs.txt").write_text(verbs)
    monkeypatch.setattr(provision, "workspace", Shared(checkout).workspace)

    answer = stack_run.product_verb(
        Places.under(tmp_path), settings(), SLOT, ["restart", "web"], checkout, run=_never, timeout=5, request=REQUEST
    )

    written = checkout / "scripts" / "ran.json"
    assert (answer, written.read_text() if written.exists() else None) == (status, ran)


@pytest.mark.parametrize(
    ("verbs", "seconds", "status", "said"),
    [
        ("up down verbs", "0", lf.REFUSED, "slot 2 ran nothing: this product's scripts/dev has no run verb"),
        ("up run verbs", "0", 0, ""),
        ("up run verbs", "30", lf.TIMED_OUT, "removed the run's 1 container(s) on slot 2"),
    ],
    ids=["no-run-verb", "ran", "past-its-time"],
)
def test_a_run_needs_the_products_run_verb_and_one_past_its_time_leaves_no_container(
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    docker_log: Path,
    verbs: str,
    seconds: str,
    status: int,
    said: str,
) -> None:
    (checkout / "scripts" / "dev").write_text(RUN_DEV)
    (checkout / "scripts" / "verbs.txt").write_text(verbs)
    monkeypatch.setattr(provision, "workspace", Shared(checkout).workspace)
    run = ["run", "web", "--", "sleep", seconds]

    answer = stack_run.product_verb(
        Places.under(tmp_path), settings(), SLOT, run, checkout, run=_never, timeout=1, request=REQUEST
    )

    calls = docker_log.read_text().splitlines() if docker_log.exists() else []
    ran = checkout / "scripts" / "ran.json"
    assert answer == status and said in capsys.readouterr().out
    assert (ran.read_text() if ran.exists() else None) == (None if status == lf.REFUSED else f"software.run={REQUEST}")
    # Only the run's own container goes: another run, or a job the stack started, carries no such label.
    gone = [f"container ls -aq --filter label=software.run={REQUEST}", "container rm -f c-web"]
    checked = 0 if status == lf.REFUSED else 2
    assert calls[checked:] == (gone if status == lf.TIMED_OUT else [])


@pytest.mark.parametrize(
    ("code", "listed", "privileged", "mounts", "said"),
    [
        (0, "c-web", False, ["/var/run/docker.sock", "/var/run/docker.sock"], "web's container commands this machine"),
        (0, "c-web", False, ["/var/run", "/host/run"], "web's container commands this machine's Docker"),
        (0, "c-web", True, [], "web's container commands this machine's Docker"),
        (0, "", False, [], "slot 2 ran nothing: it has no web container yet"),
        (1, "", False, [], "docker could not say what web's containers mount"),
        (0, "c-web", False, ["/srv/notes/media", "/media"], ""),
    ],
    ids=["its-socket", "a-folder-holding-it", "privileged", "not-up", "docker-failed", "plain"],
)
def test_a_run_is_refused_in_a_service_whose_container_commands_this_machines_docker(
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    listed: str,
    privileged: bool,
    mounts: list[str],
    said: str,
) -> None:
    (checkout / "scripts" / "dev").write_text(RUN_DEV)
    (checkout / "scripts" / "verbs.txt").write_text("up run verbs")
    monkeypatch.setattr(provision, "workspace", Shared(checkout).workspace)
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        told = json.dumps(["c-web", "/notes-slot2-web", privileged, *mounts])
        return subprocess.CompletedProcess(args, code, told if args[:2] == ["container", "inspect"] else listed, "")

    monkeypatch.setattr(stack_run, "_docker", docker)
    run = ["run", "web", "--", "sleep", "0"]

    answer = stack_run.product_verb(
        Places.under(tmp_path), settings(), SLOT, run, checkout, run=_never, timeout=5, request=REQUEST
    )

    # Python or curl on the socket command Docker as `docker` does: the service is refused, whatever the command.
    assert (answer, (checkout / "scripts" / "ran.json").exists()) == ((lf.REFUSED, False) if said else (0, True))
    assert said in capsys.readouterr().out
    service = ["--filter", "label=software.slot=2", "--filter", "label=com.docker.compose.service=web"]
    assert asked[0] == ["container", "ls", "-aq", *service]


@pytest.mark.parametrize(
    ("words", "socket", "code", "status", "said"),
    [
        (["notes-signin-1", "--", "python3", "-c", "1"], [], 0, 0, ""),
        (["notes-signin-1", "--", "sleep", "30"], [], 0, lf.TIMED_OUT, "the command may still run in notes-signin-1"),
        (["notes-slot2-web", "--", "ls"], ["/var/run/docker.sock"] * 2, 0, lf.REFUSED, "notes-slot2-web commands this"),
        (["notes-slot3-web", "--", "ls"], [], 0, lf.REFUSED, "this slot has no container named notes-slot3-web"),
        (["notes-signin-1", "--", "ls"], [], 1, lf.REFUSED, "docker could not say what this slot's containers mount"),
        (["notes-signin-1", "ls"], [], 0, lf.REFUSED, "exec <container> -- <command>"),
    ],
    ids=["a-visit", "past-its-time", "mounts-the-socket", "another-slots", "docker-failed", "no-command"],
)
def test_an_exec_runs_only_in_a_container_of_the_slot_that_cannot_command_docker(
    capsys: pytest.CaptureFixture[str],
    docker_log: Path,
    words: list[str],
    socket: list[str],
    code: int,
    status: int,
    said: str,
) -> None:
    told = [["c-visit", "/notes-signin-1", False], ["c-web", "/notes-slot2-web", False, *socket]]
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        rows = "\n".join(json.dumps(row) for row in told) if args[:2] == ["container", "inspect"] else "c-visit c-web"
        return subprocess.CompletedProcess(args, code, rows, "Cannot connect to the Docker daemon" if code else "")

    answer = docker_reach.exec_in("software.slot=2", words, docker, 1)

    calls = docker_log.read_text().splitlines() if docker_log.exists() else []
    assert (answer, calls) == (status, [] if status == lf.REFUSED else [f"exec c-visit {' '.join(words[2:])}"])
    assert said in capsys.readouterr().out
    assert asked[:1] in ([], [["container", "ls", "-aq", "--filter", "label=software.slot=2"]])


@pytest.mark.parametrize(
    ("verb", "update", "status", "said"),
    [
        ("up", (0, ""), 0, "slot 2's 2 running container(s) take 64 CPU shares to prod's 1024 each"),
        ("desktop", (0, ""), 0, "slot 2's 2 running container(s) take 64 CPU shares"),
        ("up", (1, "Error: No such container: c-gone\n"), 0, "take 64 CPU shares"),
        ("up", (1, "Cannot connect to the Docker daemon\n"), 1, "may share the CPU with prod's as equals: Cannot"),
        ("down", (0, ""), 0, ""),
    ],
    ids=["up", "desktop", "one-gone-since", "docker-failed", "down"],
)
def test_a_verb_that_starts_a_slots_containers_puts_them_below_prods_on_the_cpu(
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    update: tuple[int, str],
    status: int,
    said: str,
) -> None:
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        if args[:2] == ["container", "ls"]:
            return subprocess.CompletedProcess(args, 0, "c-web\nc-gone\n", "")
        return subprocess.CompletedProcess(args, update[0], "", update[1])

    monkeypatch.setattr(stack_run, "_docker", docker)
    monkeypatch.setenv("DEV_EXIT", "0")

    assert _verb(tmp_path, checkout, Shared(checkout), monkeypatch, verb) == status

    lowered = [["container", "ls", "-q", "--filter", "label=software.slot=2"],
               ["container", "update", "--cpu-shares", "64", "c-web", "c-gone"]]  # fmt: skip
    assert asked == (lowered if said else [])
    assert said in capsys.readouterr().out


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root), *args], check=True)


@pytest.mark.parametrize(
    ("change", "skipped"),
    [
        ("nothing", True),
        ("edited", False),
        ("untracked", False),
        ("committed", False),
        ("restarted", False),
        ("unhealthy", False),
        ("stale", False),
        ("off", False),
        ("first-failed", False),
    ],  # fmt: skip
)
def test_an_up_that_would_change_nothing_runs_nothing(
    tmp_path: Path,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
    skipped: bool,
) -> None:
    (checkout / "app.py").write_text("print(1)\n")
    (checkout / ".gitignore").write_text("ran.json\n")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        _git(checkout, *args)
    # Docker gives a container without a healthcheck no Health key at all.
    started = {"c-web": {"StartedAt": "2026-10-18T04:00:00Z", "Health": {"Status": "healthy"}},
               "c-db": {"StartedAt": "2026-10-18T04:00:01Z"}}  # fmt: skip

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        inspect = args[:2] == ["container", "inspect"]
        listed = "".join(f"{c} {json.dumps(state)}\n" for c, state in started.items()) if inspect else ""
        return subprocess.CompletedProcess(args, 0, listed or "".join(f"{c}\n" for c in started), "")

    monkeypatch.setattr(stack_run, "_docker", docker)
    monkeypatch.setattr(provision, "workspace", Shared(checkout).workspace)
    monkeypatch.setenv("DEV_EXIT", "1" if change == "first-failed" else "0")
    minutes = 0 if change == "off" else 240
    up = [Places.under(tmp_path), settings(**{"slots.same_up_minutes": minutes}), SLOT, ["up"], checkout]
    stack_run.product_verb(*up, run=_never, timeout=60, request=REQUEST)
    (checkout / "scripts" / "ran.json").unlink()
    monkeypatch.setenv("DEV_EXIT", "0")
    if change == "edited":
        (checkout / "app.py").write_text("print(2)\n")
    if change in ("untracked", "committed"):
        (checkout / "new.py").write_text("print(3)\n")
    if change == "committed":
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "next")
    if change in ("restarted", "unhealthy"):
        at, health = ("04:09", "healthy") if change == "restarted" else ("04:00", "unhealthy")
        started["c-web"] = {"StartedAt": f"2026-10-18T{at}:00Z", "Health": {"Status": health}}
    if change == "stale":
        monkeypatch.setattr(same_up, "time", SimpleNamespace(time=lambda: time.time() + 240 * 60))
    capsys.readouterr()

    assert stack_run.product_verb(*up, run=_never, timeout=60, request=REQUEST) == 0

    assert (checkout / "scripts" / "ran.json").exists() is not skipped
    assert ("slot 2 ran nothing: its last good up" in capsys.readouterr().out) is skipped
