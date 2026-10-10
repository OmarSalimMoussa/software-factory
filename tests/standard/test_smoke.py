from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from standard import config as settings
from standard.probe import Result
from standard.smoke import Smoke, SmokeError

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SHORT = COMMIT[:8]
RUNNING = {"State": {"Status": "running", "ExitCode": 0}, "RestartCount": 0}
EXITED = {"State": {"Status": "exited", "ExitCode": 3}, "RestartCount": 0}


def label(argv: list[str]) -> str:
    """A short name for one docker call, so a test can read the order of steps."""
    if argv[:2] == ["docker", "compose"]:
        rest = argv[argv.index("-f") + 2 :]
        return f"compose {rest[0]}" + (f" {rest[-1]}" if rest[0] == "logs" else "")
    if argv[:3] == ["docker", "run", "--rm"]:
        return "probe " + urlsplit(argv[-2]).path
    if argv[:3] == ["docker", "run", "-d"]:
        return "start postgres"
    if argv[:2] in (["docker", "network"], ["docker", "container"]):
        return " ".join(argv[1:4])
    if argv[:2] == ["docker", "exec"]:
        return argv[3]
    if argv[:2] == ["docker", "rm"]:
        return "remove postgres"
    return argv[1] if argv[0] == "docker" else "check"


class Docker:
    """A fake docker for the fixture product: every call recorded, each answered as the scenario says."""

    def __init__(
        self, config: settings.Config, repo: Path, *, fail: str, state: object, commit: str, taken: bool
    ) -> None:
        self.config, self.repo, self.fail, self.state, self.commit = config, repo, fail, state, commit
        self.taken = taken  # a container named factory-postgres runs before the test starts
        self.calls: list[list[str]] = []
        self.check_env: Mapping[str, str] = {}
        self.env_text = ""
        self.linked_at_config = False

    def __call__(
        self, argv: list[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None, stream: bool = False
    ) -> Result:
        self.calls.append(argv)
        step = label(argv)
        if step == self.fail:
            return Result(1, "", f"{step} broke")
        if step == "compose config":
            self.linked_at_config = (self.repo / self.config.env.file).is_symlink()
            image = self.config.image("app", SHORT)
            networks: dict[str, dict[str, Any]] = {"default": {"name": "notes-smoke_default"}}
            networks |= {n: {"name": f"notes-smoke-{n}", "external": True} for n in ("services", "extra")}
            return Result(0, json.dumps({"services": {"app": {"build": {}, "image": image}}, "networks": networks}), "")
        if step == "container inspect factory-postgres":
            return Result(0 if self.taken else 1, "", "")
        if step.startswith("network inspect"):
            made = ["docker", "network", "create", argv[3]] in self.calls
            return Result(0 if made else 1, "", "")
        if step == "compose ps":
            return Result(0, "c0ffee\n", "")
        if step == "inspect":
            return Result(0, json.dumps([self.state]), "")
        if step.startswith("probe "):
            return Result(0, self.answer(step.removeprefix("probe ")), "")
        if step == "check":
            assert env is not None
            self.check_env = env
            self.env_text = Path(env["FACTORY_ENV_FILE"]).read_text()
        return Result(0, "", "")

    def answer(self, path: str) -> str:
        if path == self.config.deploy.health:
            return "200\n" + json.dumps({"workspace": "smoke", "build_commit": self.commit, "busy": 0})
        return f"{next(e.status for e in self.config.smoke.expect if e.path == path)}\n"


@pytest.fixture
def product(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    repo.mkdir()
    for name in ("factory.toml", "compose.yml", "notes.env.template"):
        shutil.copy(FIXTURE / name, repo / name)
    return repo


def smoke(
    product: Path, fail: str = "", state: object = RUNNING, commit: str = SHORT, taken: bool = False
) -> tuple[Docker, list[str], list[float], SmokeError | None]:
    config = settings.load(product)
    docker = Docker(config, product, fail=fail, state=state, commit=commit, taken=taken)
    said: list[str] = []
    slept: list[float] = []
    clock = iter(range(10_000))
    run = Smoke(config, product, COMMIT, docker, clock=lambda: float(next(clock)), sleep=slept.append, say=said.append)
    try:
        run.test()
    except SmokeError as exc:
        return docker, said, slept, exc
    return docker, said, slept, None


TEARDOWN = [
    "compose down",
    "remove postgres",
    "network rm notes-smoke-extra",
    "network rm notes-smoke-telemetry",
    "network rm notes-smoke-services",
]


def test_the_steps_run_in_order_against_a_scratch_workspace_and_leave_nothing_behind(product: Path) -> None:
    docker, said, slept, failure = smoke(product)
    expect = settings.load(product).smoke.expect[0].path

    assert failure is None, failure
    assert [label(argv) for argv in docker.calls] == [
        "container inspect factory-postgres",
        "network create notes-smoke-services",
        "network create notes-smoke-telemetry",
        "pull",
        "start postgres",
        "pg_isready",
        "compose config",
        "network inspect notes-smoke-services",
        "network inspect notes-smoke-extra",
        "network create notes-smoke-extra",
        "compose pull",
        "compose up",
        "compose ps",
        "inspect",
        f"probe {expect}",
        "inspect",
        "probe /healthz",
        "check",
        "inspect",
        *TEARDOWN,
    ]
    assert docker.linked_at_config and not (product / "notes.env").exists()
    assert {k: docker.check_env[k] for k in ("FACTORY_PROJECT", "FACTORY_WORKSPACE", "FACTORY_COMMIT")} == {
        "FACTORY_PROJECT": "notes-smoke",
        "FACTORY_WORKSPACE": "smoke",
        "FACTORY_COMMIT": SHORT,
    }
    # Telemetry points at a dead port, so the stack proves it boots without its sinks.
    for line in (
        "LANGFUSE_HOST=http://127.0.0.1:9",
        "SENTRY_DSN_APP=http://notes@127.0.0.1:9/1",
        f"NOTES_IMAGE_TAG={SHORT}",
    ):
        assert line in docker.env_text.splitlines()
    assert "DATABASE_URL=postgresql://notes_smoke:" in docker.env_text
    assert not slept and said[-1].startswith("smoke: PASS")


@pytest.mark.parametrize(
    ("scenario", "message", "teardown"),
    [
        ({"fail": "compose up"}, "could not start the stack (exit code 1): compose up broke", TEARDOWN),
        ({"state": EXITED}, "app stopped (exited, exit code 3)", TEARDOWN),
        ({"commit": "deadbeef"}, f"build_commit is 'deadbeef', not this commit '{SHORT}'", TEARDOWN),
        ({"fail": "check"}, "the product's check scripts/smoke-check failed (exit code 1)", TEARDOWN),
        (
            {"fail": "compose config"},
            "could not read compose.yml",
            ["remove postgres", "network rm notes-smoke-telemetry", "network rm notes-smoke-services"],
        ),
        ({"taken": True}, "a container named factory-postgres already runs here", []),
    ],
)
def test_a_failure_stops_at_once_prints_the_logs_and_still_tears_down(
    product: Path, scenario: dict[str, Any], message: str, teardown: list[str]
) -> None:
    docker, said, slept, failure = smoke(product, **scenario)

    assert failure is not None and message in str(failure)
    assert not slept, "a failure that is already certain never waits out the timeout"
    started = "compose down" in teardown
    assert any("the last 50 lines of app" in line for line in said) == started
    steps = [label(argv) for argv in docker.calls]
    assert steps[len(steps) - len(teardown) :] == teardown
    # Only the scratch Postgres is ever removed, never one that ran before the test.
    assert steps.count("remove postgres") == teardown.count("remove postgres")
    assert not (product / "notes.env").exists()
