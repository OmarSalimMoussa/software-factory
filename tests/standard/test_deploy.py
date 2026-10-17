from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from deploy_kit import COMPOSE, FACTORY_TOML, SERVICES_ENV, TEMPLATE, FakeClock, FakeDocker, FakeHealth, commit

from standard.server.deploy import main
from standard.server.deploy_inputs import Paths, Tools

DRAIN = "http://127.0.0.1:8300/healthz"
ORIGIN = "https://notes.example.com/healthz"
FILES = {
    "compose.yml": COMPOSE,
    "notes.env.template": TEMPLATE,
    "factory.toml": FACTORY_TOML,
    "hooks/after.sh": "#!/bin/sh\n",
    "app/Dockerfile": "FROM python:3.12-slim\n",
}


@dataclass
class Server:
    root: Path
    docker: FakeDocker
    health: FakeHealth
    sha: str = ""
    clock: FakeClock = field(default_factory=FakeClock)
    hook_exit: int = 0
    wait_seconds: int = 3600

    @property
    def prod(self) -> Path:
        return self.root / "srv" / "notes" / "prod"

    @property
    def product_toml(self) -> Path:
        return self.root / "etc" / "product.toml"

    @property
    def short(self) -> str:
        return self.sha[:8]

    def commit(self, files: dict[str, str]) -> str:
        self.sha = commit(self.root / "repo", files)
        self.health.answers[DRAIN] = [None]
        self.health.answers[ORIGIN] = [(200, {"workspace": "prod", "build_commit": self.short, "busy": 0})]
        return self.sha

    def hook(self, argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, self.hook_exit, f"probed {env['FACTORY_COMMIT']}\n", "")

    def deploy(self, *argv: str) -> int:
        paths = Paths(
            mirror=self.root / "repo" / ".git",
            srv=self.root / "srv",
            lock=self.root / "srv" / "factory" / "state" / "deploy.lock",
            services_env=self.root / "srv" / "factory" / "services" / "services.env",
            edge_sites=self.root / "srv" / "factory" / "edge" / "sites",
            product_toml=self.product_toml,
            gc_log=self.root / "srv" / "factory" / "state" / "gc.log",
        )
        tools = Tools(
            run=self.docker,
            get=self.health,
            hook=self.hook,
            now=self.clock.now,
            sleep=self.clock.sleep,
            utc=lambda: datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        )
        return main(list(argv), paths, self.wait_seconds, tools)

    def steps(self) -> list[str]:
        return [" ".join(argv[1:3]) for argv in self.docker.argvs()]


@pytest.fixture
def server(tmp_path: Path) -> Server:
    files = {
        "etc/product.toml": FACTORY_TOML,
        "srv/factory/services/services.env": SERVICES_ENV,
        "srv/notes/prod/env/notes.env": "MAIL_HOST=mail.example.com\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    made = Server(tmp_path, FakeDocker(networks={"bridge": set()}), FakeHealth({}))
    made.commit(FILES)
    return made


def test_a_push_builds_the_commit_takes_its_files_in_place_and_ends_with_the_deployed_line(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = server.prod / "stack"
    stack.mkdir(parents=True)
    (stack / "compose.yml").write_text("old\n")
    (stack / "gone.txt").write_text("from an older commit\n")
    (server.prod / "state").mkdir()
    (server.prod / "state" / "deployed").write_text("12345678" + "0" * 32 + "\n")
    inode = (stack / "compose.yml").stat().st_ino

    assert server.deploy(server.sha) == 0

    assert capsys.readouterr().out.splitlines()[-1] == f"deployed prod {server.short} previous 12345678"
    assert (stack / "compose.yml").stat().st_ino == inode and (stack / "compose.yml").read_text() == COMPOSE
    assert not (stack / "gone.txt").exists() and not (stack / "app").exists()
    assert (stack / "notes.env").readlink() == Path("../env/notes.env")
    assert "NOTES_WORKSPACE=prod\n" in (server.prod / "env" / "notes.env").read_text()
    assert (server.root / "srv/factory/edge/sites/notes-prod.caddy").exists()
    record = json.loads((server.prod / "state" / "history.jsonl").read_text())
    previous = "12345678" + "0" * 32
    assert record == {"at": "2026-10-10T12:00:00Z", "commit": server.sha, "previous": previous, "back": False}
    steps = server.steps()
    assert not [step for step in steps if step.split()[0] in ("login", "pull", "push", "logout")]
    order = ["build --tag", "exec -i", "compose -p", "exec factory-edge", "image prune"]
    firsts = [steps.index(step) for step in order]
    assert firsts == sorted(firsts)
    log = (server.prod / "state" / "deploy.log").read_text()
    assert f"2026-10-10T12:00:00Z deployed prod {server.short} previous 12345678\n" in log


CHANGED = FACTORY_TOML.replace("/admin/export*", "/old-admin*")


@pytest.mark.parametrize(
    ("toml", "back", "trouble", "said"),
    [
        (CHANGED, False, "", ""),
        ("[product\n", False, "", "factory.toml at {short}: is not valid TOML"),
        ("[product\n", True, "", ""),
        (CHANGED, False, "read-only", "product.toml is not this user's to rewrite"),
        (CHANGED, False, "build-fails", "failed (exit 1): ERROR: failed to solve"),
    ],
    ids=["push-refreshes", "push-of-a-file-that-does-not-parse",
         "back-keeps-the-servers-copy", "servers-copy-not-the-admins", "build-fails"],
)  # fmt: skip
def test_a_push_makes_the_servers_copy_its_factory_toml_once_prod_runs_it_and_a_push_back_keeps_it(
    server: Server, capsys: pytest.CaptureFixture[str], toml: str, back: bool, trouble: str, said: str
) -> None:
    sha = server.commit({"factory.toml": toml})
    if trouble == "read-only":
        server.product_toml.chmod(0o444)
    if trouble == "build-fails":
        server.docker.build_exit = 1

    assert server.deploy(sha, *(["back"] if back else [])) == (1 if said else 0)

    out = capsys.readouterr().out
    kept = back or bool(said)
    assert server.product_toml.read_text() == (FACTORY_TOML if kept else toml)
    if said:
        assert said.format(short=server.short) in out
        assert not [step for step in server.steps() if step.startswith("compose")]
    else:
        site = (server.root / "srv/factory/edge/sites/notes-prod.caddy").read_text()
        assert ("respond /old-admin* 404" in site) is not kept and ("/admin/export*" in site) is kept
        assert json.loads((server.prod / "state" / "history.jsonl").read_text())["back"] is back


def test_a_build_that_leaves_too_little_time_for_the_drain_and_the_answer_fails_with_prod_untouched(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.wait_seconds = 3600
    server.docker.build_seconds, server.docker.clock = 3550, server.clock  # the drain and the answer need 80 s

    assert server.deploy(server.sha) == 1

    assert "the build took too long to leave time for the drain and the answer" in capsys.readouterr().out
    assert [step for step in server.steps() if step.startswith("compose")] == []


def test_a_commit_the_warden_s_mirror_lacks_is_refused_before_anything_runs(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert server.deploy("f" * 40) == 1

    assert f"failed: the warden's mirror holds no commit {'f' * 40}" in capsys.readouterr().out
    assert server.docker.calls == [] and not server.prod.joinpath("state").exists()


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

    assert server.deploy(server.sha) == code

    assert server.clock.slept == slept
    assert says in capsys.readouterr().out
    assert ("compose -p" in server.steps()) == (code == 0)


def test_the_answer_check_names_the_commit_that_answered(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.health.answers[ORIGIN] = [(200, {"workspace": "prod", "build_commit": "deadbeef"})]

    assert server.deploy(server.sha) == 1

    said = f"failed: {ORIGIN} answers prod at deadbeef, not prod at {server.short}, after 20 s"
    assert said in capsys.readouterr().out
    assert not (server.prod / "state" / "deployed").exists()


def test_a_failed_after_deploy_hook_keeps_every_image(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.hook_exit = 3
    server.docker.images = ["ghcr.io/owner/notes-app:11111111"]

    assert server.deploy(server.sha) == 1

    out = capsys.readouterr().out
    assert f"hooks/after.sh: probed {server.short}" in out and "after-deploy hook hooks/after.sh failed (exit 3)" in out
    assert not [s for s in server.steps() if s in ("image rm", "image prune")]
    assert (server.prod / "state" / "deployed").read_text() == server.sha + "\n"


def test_prune_keeps_the_last_commits_of_each_workspace_and_images_in_use(server: Server) -> None:
    for ws, commits in (("prod", ["11111111", "22222222", "33333333"]), ("staging", ["44444444", "55555555"])):
        state = server.root / "srv" / "notes" / ws / "state"
        state.mkdir(parents=True)
        lines = [json.dumps({"commit": c + "0" * 32}) for c in commits]
        (state / "history.jsonl").write_text("\n".join(lines) + "\n")
    app = "ghcr.io/owner/notes-app"
    server.docker.images = [
        *(f"{app}:{tag}" for tag in ["11111111", "22222222", "33333333", server.short, "44444444", "55555555"]),
        f"{app}:buildcache",
        "ghcr.io/owner/notes-deploy:11111111",
        "ghcr.io/other/notes-app:11111111",
    ]
    server.docker.in_use = [f"{app}:11111111"]

    assert server.deploy(server.sha) == 0

    assert "build --tag" not in server.steps()
    assert [argv[3] for argv in server.docker.argvs() if argv[1:3] == ["image", "rm"]] == [
        f"{app}:22222222",
        "ghcr.io/owner/notes-deploy:11111111",
    ]


@pytest.mark.parametrize(
    "argv",
    [[], ["abcdef12"], ["ABCDEF12" + "0" * 32], ["a" * 40, "follow"], ["a" * 40, "back", "back"], ["prod", "a" * 40]],
)
def test_arguments_other_than_one_full_commit_and_back_are_refused(server: Server, argv: list[str]) -> None:
    assert server.deploy(*argv) == 2
    assert server.docker.calls == []


def test_a_service_restarts_only_when_a_file_it_mounts_changed(server: Server) -> None:
    proxy = "services:\n  proxy:\n    image: proxy\n    volumes: [./proxy.conf:/c:ro]\n"
    files = '"compose.yml", "notes.env.template"'
    toml = FACTORY_TOML.replace(f"files = [{files}]", f'files = [{files}, "proxy.conf"]')
    compose = COMPOSE.replace("services:\n", proxy, 1)
    sha = server.commit({"compose.yml": compose, "proxy.conf": "first\n", "factory.toml": toml})

    def restarts() -> list[list[str]]:
        return [argv for argv in server.docker.argvs() if argv[:2] == ["docker", "compose"] and "restart" in argv]

    assert server.deploy(sha) == 0
    assert [argv[argv.index("restart") :] for argv in restarts()] == [["restart", "proxy"]]
    server.docker.calls.clear()
    server.health.answers[DRAIN] = [None]

    assert server.deploy(sha) == 0
    assert restarts() == []


def test_a_failed_weekly_clean_up_shows_in_every_deploy_until_one_passes(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    log = server.root / "srv" / "factory" / "state" / "gc.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-10-04T03:00:00Z FAILED at pruning the build cache: no space left on device\n")

    server.deploy(server.sha)

    assert "warning: the weekly clean-up failed (2026-10-04T03:00:00Z FAILED at pruning" in capsys.readouterr().out
