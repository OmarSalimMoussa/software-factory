from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from system_fakes import FIXTURE, settings

from runtime.shared.paths import SLOT_KEYS, Places
from runtime.system.runners import launch_file as lf
from runtime.system.runners import stack_run
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
SERVICES = (
    "POSTGRES_PASSWORD=p\nLANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk\nLANGFUSE_INIT_PROJECT_SECRET_KEY=sk\n"
    "GLITCHTIP_OWNER_PASSWORD=g\n"
)


def _never(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the fakes run nothing, yet {argv} ran")


class Shared:
    """provision.workspace and provision.drop, recording each call."""

    def __init__(self, checkout: Path, fail: bool = False) -> None:
        self.checkout, self.fail = checkout, fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def workspace(self, config: object, services: dict[str, str], run: object, **kw: Any) -> dict[str, str]:
        if self.fail:
            raise provision.ProvisionError("GlitchTip (factory-glitchtip) refused: no such organization")
        self.calls.append(("workspace", kw))
        return {"FACTORY_SERVICES_NETWORK": "notes-slot2-services", "DATABASE_URL": "postgresql://n:pw@db:5432/n"}

    def drop(self, config: object, services: dict[str, str], run: object, **kw: Any) -> None:
        assert (self.checkout / "scripts" / "ran.json").is_file(), "dropped before scripts/dev ran"
        self.calls.append(("drop", kw))


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    for name in ("factory.toml", "notes.env.template"):
        shutil.copy(FIXTURE / name, root / name)
    (root / "scripts" / "dev").write_text(DEV)
    (root / "scripts" / "dev").chmod(0o755)
    places = Places.under(tmp_path)
    places.services.mkdir(parents=True)
    (places.services / "services.env").write_text(SERVICES)
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


# docker as a stand-in: logs each call and answers a listing; slot 2's network still holds two unlabelled containers.
DOCKER = f"""#!{sys.executable}
import os, sys
with open(os.environ["DOCKER_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
answers = {{"container ls": "c-web", "network ls": "n-slot2", "network inspect": "agent-7 agent-8 "}}
print(answers.get(" ".join(sys.argv[1:3]), ""))
"""


def test_the_sweep_removes_the_containers_still_on_a_slots_network_before_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").write_text(DOCKER)
    (tmp_path / "bin" / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("DOCKER_LOG", str(tmp_path / "docker.log"))

    status = stack_run.sweep(SLOT, 60)

    removals = [line for line in (tmp_path / "docker.log").read_text().splitlines() if " rm " in f" {line} "]
    assert status == 0
    assert removals == ["container rm -f c-web", "container rm -f agent-7 agent-8", "network rm n-slot2"]
    assert "removed 2 container(s) still on slot 2's network n-slot2: agent-7, agent-8" in capsys.readouterr().out


def test_the_events_verb_prints_each_slot_containers_stop_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = {
        "Action": "die",
        "timeNano": 1791000000000000000,
        "Actor": {"ID": "0123456789abcdef", "Attributes": {"name": "web", "software.slot": "2", "exitCode": "137"}},
    }
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(seen) + "\n", "")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.events("1791000000", "1791000300", 30) == 0
    assert stack_run.events("yesterday", "1791000300", 30) == lf.REFUSED

    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert printed == [{"action": "die", "at": 1791000000000000000, "id": "0123456789ab", "name": "web", "slot": "2",
                        "exit": "137"}]  # fmt: skip
    assert asked[0][:5] == ["events", "--since", "1791000000", "--until", "1791000300"] and len(asked) == 1


def test_the_nightly_prune_removes_only_slot_images_since_prods_share_the_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        asked.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(stack_run, "_docker", docker)

    assert stack_run.prune("168", 30) == 0

    images = [args for args in asked if args[0] == "image"]
    assert images == [["image", "prune", "-a", "-f", "--filter", "label=software.slot", "--filter", "until=168h"]]
    assert [args[0] for args in asked] == ["builder", "image"]


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
    verbs: str,
    seconds: str,
    status: int,
    said: str,
) -> None:
    (checkout / "scripts" / "dev").write_text(RUN_DEV)
    (checkout / "scripts" / "verbs.txt").write_text(verbs)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").write_text(DOCKER)
    (tmp_path / "bin" / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("DOCKER_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setattr(provision, "workspace", Shared(checkout).workspace)
    run = ["run", "web", "--", "sleep", seconds]

    answer = stack_run.product_verb(
        Places.under(tmp_path), settings(), SLOT, run, checkout, run=_never, timeout=1, request=REQUEST
    )

    log = tmp_path / "docker.log"
    calls = log.read_text().splitlines() if log.exists() else []
    ran = checkout / "scripts" / "ran.json"
    assert answer == status and said in capsys.readouterr().out
    assert (ran.read_text() if ran.exists() else None) == (None if status == lf.REFUSED else f"software.run={REQUEST}")
    # Only the run's own container goes: another run, or a job the stack started, carries no such label.
    gone = [f"container ls -aq --filter label=software.run={REQUEST}", "container rm -f c-web"]
    assert calls == (gone if status == lf.TIMED_OUT else [])
