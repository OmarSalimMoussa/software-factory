from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from deploy_kit import COMPOSE, FACTORY_TOML, SERVICES_ENV, TEMPLATE, FakeClock, FakeDocker, FakeHealth

from standard.server.deploy import Paths, Tools, main

SHA = "abcdef12" + "0" * 32
FACTORY = "f" * 40
DRAIN = "http://127.0.0.1:8300/healthz"
ORIGIN = "https://notes.example.com/healthz"
LIVE = (200, {"workspace": "prod", "build_commit": "abcdef12", "busy": 0})


@dataclass
class Server:
    root: Path
    docker: FakeDocker
    health: FakeHealth
    clock: FakeClock = field(default_factory=FakeClock)
    hook_exit: int = 0

    @property
    def image(self) -> Path:
        return self.root / "image" / "deploy"

    @property
    def prod(self) -> Path:
        return self.root / "srv" / "notes" / "prod"

    def hook(self, argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, self.hook_exit, f"probed {env['FACTORY_COMMIT']}\n", "")

    def deploy(self, *argv: str) -> int:
        paths = Paths(
            srv=self.root / "srv",
            lock=self.root / "srv" / "factory" / "state" / "deploy.lock",
            services_env=self.root / "srv" / "factory" / "services" / "services.env",
            edge_sites=self.root / "srv" / "factory" / "edge" / "sites",
            factory_commit=self.root / "lib" / "COMMIT",
            product_toml=self.root / "etc" / "product.toml",
        )
        tools = Tools(
            run=self.docker,
            get=self.health,
            hook=self.hook,
            now=self.clock.now,
            sleep=self.clock.sleep,
            utc=lambda: datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        )
        return main(list(argv), paths, tools, io.StringIO("registry-token\n"))

    def steps(self) -> list[str]:
        return [" ".join(argv[1:3]) for argv in self.docker.argvs()]


@pytest.fixture
def server(tmp_path: Path) -> Server:
    files = {
        "etc/product.toml": FACTORY_TOML,
        "lib/COMMIT": FACTORY + "\n",
        "srv/factory/services/services.env": SERVICES_ENV,
        "srv/notes/prod/env/notes.env": "MAIL_HOST=mail.example.com\n",
        "image/deploy/compose.yml": COMPOSE,
        "image/deploy/notes.env.template": TEMPLATE,
        "image/deploy/factory.toml": FACTORY_TOML,
        "image/deploy/hooks/after.sh": "#!/bin/sh\n",
        "image/deploy/COMMIT": SHA + "\n",
        "image/deploy/plan.json": json.dumps(
            {"factory_commit": FACTORY, "images": [{"name": "app", "ref": "ghcr.io/owner/notes-app"}]}
        ),
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    (tmp_path / "image/deploy/hooks/after.sh").chmod(0o755)
    docker = FakeDocker(deploy_files=tmp_path / "image" / "deploy", networks={"bridge": set()})
    return Server(tmp_path, docker, FakeHealth({DRAIN: [None], ORIGIN: [LIVE]}))


def test_a_deploy_syncs_files_in_place_and_ends_with_the_deployed_line(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = server.prod / "stack"
    stack.mkdir(parents=True)
    (stack / "compose.yml").write_text("old\n")
    (stack / "gone.txt").write_text("from an older commit\n")
    (server.prod / "state").mkdir()
    (server.prod / "state" / "deployed").write_text("12345678" + "0" * 32 + "\n")
    inode = (stack / "compose.yml").stat().st_ino

    assert server.deploy("prod", "abcdef12") == 0

    assert capsys.readouterr().out.splitlines()[-1] == "deployed prod abcdef12 previous 12345678"
    assert (stack / "compose.yml").stat().st_ino == inode and (stack / "compose.yml").read_text() == COMPOSE
    assert not (stack / "gone.txt").exists()
    assert (stack / "notes.env").readlink() == Path("../env/notes.env")
    assert "NOTES_WORKSPACE=prod\n" in (server.prod / "env" / "notes.env").read_text()
    assert (server.root / "srv/factory/edge/sites/notes-prod.caddy").exists()
    record = json.loads((server.prod / "state" / "history.jsonl").read_text())
    assert record == {"at": "2026-10-10T12:00:00Z", "commit": SHA, "previous": "12345678" + "0" * 32, "mode": None}
    steps = server.steps()
    assert steps[0] == "login ghcr.io" and steps[-1] == "logout ghcr.io"
    firsts = [steps.index(step) for step in ["pull -q", "exec -i", "compose -p", "exec factory-edge", "image prune"]]
    assert firsts == sorted(firsts)
    log = (server.prod / "state" / "deploy.log").read_text()
    assert "2026-10-10T12:00:00Z deployed prod abcdef12 previous 12345678\n" in log


def test_a_pinned_staging_ignores_a_follow_and_touches_nothing(server: Server) -> None:
    pinned = server.root / "srv/notes/staging/state/pinned"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("feature/x\n")

    assert server.deploy("staging", "abcdef12", "follow") == 0

    assert server.docker.calls == []
    assert sorted(p.name for p in pinned.parent.parent.iterdir()) == ["state"]
    assert pinned.read_text() == "feature/x\n"


@pytest.mark.parametrize(
    ("file", "text", "says"),
    [
        ("COMMIT", "9" * 40 + "\n", "holds commit '9999"),
        ("plan.json", json.dumps({"factory_commit": "e" * 40, "images": []}), "the server runs factory 'ffff"),
        ("factory.toml", FACTORY_TOML.replace("/admin/export*", "/other*"), "differs from the server's copy in [edge]"),
    ],
)
def test_a_deploy_image_that_does_not_match_the_server_is_refused(
    server: Server, capsys: pytest.CaptureFixture[str], file: str, text: str, says: str
) -> None:
    (server.image / file).write_text(text)

    assert server.deploy("prod", "abcdef12") == 1

    assert says in capsys.readouterr().out
    assert "compose -p" not in server.steps() and server.steps()[-1] == "logout ghcr.io"


@pytest.mark.parametrize(
    ("answers", "code", "slept", "says"),
    [
        ([(200, {"busy": 2}), (200, {"busy": 1}), (200, {"busy": 0})], 0, [10, 10], "drain: nothing is busy"),
        ([(200, {"busy": 3})], 0, [10] * 6, "warning: 3 jobs are still busy after 1 min; going on"),
        ([(503, {})], 0, [], "answers 503, so nothing healthy is running"),
        ([(200, {"workspace": "prod"})], 1, [], "answered without a whole-number busy count"),
        ([(200, {"busy": True})], 1, [], "answered without a whole-number busy count"),
    ],
)
def test_the_drain_waits_on_busy_work_and_fails_on_an_answer_without_a_count(
    server: Server,
    capsys: pytest.CaptureFixture[str],
    answers: list[tuple[int, dict[str, object]] | None],
    code: int,
    slept: list[int],
    says: str,
) -> None:
    server.health.answers[DRAIN] = answers

    assert server.deploy("prod", "abcdef12") == code

    assert server.clock.slept == slept
    assert says in capsys.readouterr().out
    assert ("compose -p" in server.steps()) == (code == 0)


def test_the_answer_check_names_the_commit_that_answered(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.health.answers[ORIGIN] = [(200, {"workspace": "prod", "build_commit": "deadbeef"})]

    assert server.deploy("prod", "abcdef12") == 1

    assert f"failed: {ORIGIN} answers prod at deadbeef, not prod at abcdef12, after 20 s" in capsys.readouterr().out
    assert not (server.prod / "state" / "deployed").exists()


def test_a_failed_after_deploy_hook_keeps_every_image(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.hook_exit = 3
    server.docker.images = ["ghcr.io/owner/notes-app:11111111"]

    assert server.deploy("prod", "abcdef12") == 1

    out = capsys.readouterr().out
    assert "hooks/after.sh: probed abcdef12" in out and "after-deploy hook hooks/after.sh failed (exit 3)" in out
    assert not [s for s in server.steps() if s.startswith("image")]
    assert (server.prod / "state" / "deployed").read_text() == SHA + "\n"


def test_prune_keeps_the_last_commits_of_each_workspace_and_images_in_use(server: Server) -> None:
    for ws, commits in (("prod", ["11111111", "22222222", "33333333"]), ("staging", ["44444444", "55555555"])):
        state = server.root / "srv" / "notes" / ws / "state"
        state.mkdir(parents=True)
        lines = [json.dumps({"commit": c + "0" * 32}) for c in commits]
        (state / "history.jsonl").write_text("\n".join(lines) + "\n")
    app = "ghcr.io/owner/notes-app"
    server.docker.images = [
        *(f"{app}:{tag}" for tag in ["11111111", "22222222", "33333333", "abcdef12", "44444444", "55555555"]),
        f"{app}:buildcache",
        "ghcr.io/owner/notes-deploy:11111111",
        "ghcr.io/other/notes-app:11111111",
    ]
    server.docker.in_use = [f"{app}:11111111"]

    assert server.deploy("prod", "abcdef12") == 0

    assert [argv[3] for argv in server.docker.argvs() if argv[1:3] == ["image", "rm"]] == [
        f"{app}:22222222",
        "ghcr.io/owner/notes-deploy:11111111",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["prod", "abcdef12", "follow"],
        ["staging", "abcdef12"],
        ["staging", "abcdef1", "follow"],
        ["staging", "abcdef12", "pin", "-rf"],
        ["dev", "abcdef12"],
    ],
)
def test_arguments_outside_the_three_shapes_are_refused(server: Server, argv: list[str]) -> None:
    assert server.deploy(*argv) == 2
    assert server.docker.calls == []


def test_a_service_restarts_only_when_a_file_it_mounts_changed(server: Server) -> None:
    deploy_files = server.root / "image" / "deploy"
    (deploy_files / "compose.yml").write_text(
        COMPOSE.replace("services:\n", "services:\n  proxy:\n    image: proxy\n    volumes: [./proxy.conf:/c:ro]\n", 1)
    )
    (deploy_files / "proxy.conf").write_text("first\n")

    def restarts() -> list[list[str]]:
        return [argv for argv in server.docker.argvs() if argv[:2] == ["docker", "compose"] and "restart" in argv]

    assert server.deploy("prod", "abcdef12") == 0
    assert [argv[argv.index("restart") :] for argv in restarts()] == [["restart", "proxy"]]
    server.docker.calls.clear()
    server.health.answers[DRAIN] = [None]
    server.health.answers[ORIGIN] = [LIVE]

    assert server.deploy("prod", "abcdef12") == 0
    assert restarts() == []


@pytest.mark.parametrize(("answers", "pinned"), [(False, False), (True, True)], ids=["never-answered", "answered"])
def test_a_pin_holds_only_once_its_deploy_succeeded(server: Server, answers: bool, pinned: bool) -> None:
    env = server.root / "srv/notes/staging/env/notes.env"
    env.parent.mkdir(parents=True)
    env.write_text("MAIL_HOST=mail.example.com\n")
    server.health.answers["http://127.0.0.1:8301/healthz"] = [None]
    live = (200, {"workspace": "staging", "build_commit": "abcdef12", "busy": 0})
    server.health.answers["https://staging.notes.example.com/healthz"] = [live if answers else None]

    code = server.deploy("staging", "abcdef12", "pin", "feature/x")

    assert (code == 0) is answers
    assert (server.root / "srv/notes/staging/state/pinned").exists() is pinned
