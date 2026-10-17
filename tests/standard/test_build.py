"""Prod's build on the box: only the images Docker lacks, base images first, with the plan's args, never labelled."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from deploy_kit import FakeDocker

from standard.plan import Image, Plan
from standard.server import build
from standard.server.deploy_steps import DeployError

SHA = "abcdef12" + "0" * 32
REF = "ghcr.io/owner/notes-"
BASE = Image("base", None, ".", "base/Dockerfile", None, {"NOTES_BUILD_COMMIT": "abcdef12"}, None, REF + "base")
WORKER = Image("worker", None, ".", "worker/Dockerfile", "run", {}, None, REF + "worker")
APP = Image("app", "app", "app", "app/Dockerfile", None, {"NOTES_IMAGE_TAG": "abcdef12"}, "base", REF + "app")
PLAN = Plan("notes", "ghcr.io/owner", SHA, SHA[:8], (BASE, WORKER, APP), ("compose.yml",))


def _built(docker: FakeDocker) -> list[list[str]]:
    return [argv for argv in docker.argvs() if argv[1] == "build"]


@pytest.mark.parametrize(
    ("held", "built"),
    [([], ["base", "worker", "app"]), (["worker"], ["base", "app"]), (["base", "worker", "app"], [])],
    ids=["none-here", "one-here", "all-here"],
)
def test_only_the_images_docker_lacks_build_bases_first_with_the_plans_args_and_no_label(
    tmp_path: Path, held: list[str], built: list[str]
) -> None:
    docker = FakeDocker(images=[f"{REF}{name}:abcdef12" for name in held] + [f"{REF}app:11111111"])
    said: list[str] = []

    build.images(docker, PLAN, tmp_path, said.append)

    calls = _built(docker)
    assert [argv[argv.index("--tag") + 1] for argv in calls] == [f"{REF}{name}:abcdef12" for name in built]
    assert not [argv for argv in docker.argvs() if any(word.startswith("--label") for word in argv)]
    assert said[-1] == f"images: {3 - len(built)} of 3 were here at abcdef12"
    if "app" in built:
        app = ["--file", str(tmp_path / "app/Dockerfile"), "--build-arg", "NOTES_IMAGE_TAG=abcdef12"]
        assert calls[-1][4:] == [*app, str(tmp_path / "app")]
    if "worker" in built:
        assert calls[built.index("worker")][5:8] == [str(tmp_path / "worker/Dockerfile"), "--target", "run"]


def test_a_failed_build_stops_there_naming_the_image_after_its_output(tmp_path: Path) -> None:
    docker = FakeDocker()

    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if argv[1] == "build":
            docker.calls.append((argv, stdin))
            return subprocess.CompletedProcess(argv, 1, "#5 RUN pip install\n", "ERROR: failed to solve\n")
        return docker(argv, stdin)

    said: list[str] = []
    with pytest.raises(
        DeployError, match=f"docker build of {REF}base:abcdef12 failed \\(exit 1\\): ERROR: failed to solve$"
    ):
        build.images(run, PLAN, tmp_path, said.append)

    assert said == ["build base: #5 RUN pip install", "build base: ERROR: failed to solve"]
    assert len(_built(docker)) == 1
