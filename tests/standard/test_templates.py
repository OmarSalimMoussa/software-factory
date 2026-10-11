from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from standard import config as settings
from standard import yaml_subset

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates" / "ci"
PRODUCT = ROOT / "testdata" / "fixture-product"
NAMES = ("ci.yml", "deploy-prod.yml")
SHA = "0123456789abcdef0123456789abcdef01234567"
# Stands in for ssh: keeps what the workflow sent and answers with $FAKE_LAST as the deploy's last line.
FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$@" > "$HOME/ssh-args"
cat > "$HOME/ssh-stdin"
printf 'pulling\\n%s\\n' "$FAKE_LAST"
"""


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml_subset.load((TEMPLATES / name).read_text())
    return loaded


def run_step(name: str, job: str, step: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Runs one step's script as GitHub does, from a product checkout holding only factory.toml."""
    script = next(s["run"] for s in workflow(name)["jobs"][job]["steps"] if s.get("name") == step)
    (tmp_path / "step.sh").write_text(script)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ssh").write_text(FAKE_SSH)
    (tmp_path / "bin" / "ssh").chmod(0o755)
    (tmp_path / "home").mkdir()
    shutil.copy(PRODUCT / "factory.toml", tmp_path / "factory.toml")
    env |= {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "SERVER_DEPLOY_KEY": "a private key",
        "REGISTRY_TOKEN": "a registry token",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
    }
    argv = ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(tmp_path / "step.sh")]
    return subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=30)


@pytest.mark.parametrize("name", NAMES)
def test_each_template_reads_and_the_fixture_product_carries_it_unchanged(name: str) -> None:
    assert isinstance(workflow(name)["jobs"], dict)
    assert (PRODUCT / ".github" / "workflows" / name).read_bytes() == (TEMPLATES / name).read_bytes()


def test_no_workflow_hands_a_step_every_secret() -> None:
    for name in NAMES:
        assert not re.search(r"tojson\(\s*secrets\s*\)", (TEMPLATES / name).read_text().lower()), name


def test_staging_and_prod_queue_apart_and_no_deploy_is_cancelled_half_way() -> None:
    deploys = [workflow("ci.yml")["jobs"]["deploy-staging"], workflow("deploy-prod.yml")["jobs"]["deploy"]]

    assert [job["concurrency"] for job in deploys] == [
        {"group": "deploy-staging", "cancel-in-progress": False},
        {"group": "deploy-prod", "cancel-in-progress": False},
    ]
    assert not any("concurrency" in workflow(name) for name in NAMES)


def _default_branch_at(tmp_path: Path, branch: str, tip: str) -> dict[str, str]:
    """GitHub's API, as a file the step reads: the default branch's newest commit is ``tip``; this run's is SHA."""
    ref = tmp_path / "api" / "repos" / "acme" / "shop" / "git" / "ref" / "heads" / branch
    ref.parent.mkdir(parents=True)
    ref.write_text(f'{{"object": {{"sha": "{tip}"}}}}')
    return {"COMMIT": SHA, "REPO": "acme/shop", "API": f"file://{tmp_path / 'api'}"}


@pytest.mark.parametrize(
    ("event", "tip", "deployed"),
    [("push", SHA, True), ("push", "f" * 40, False), ("workflow_dispatch", "f" * 40, True)],
    ids=["newest", "overtaken", "by-hand"],
)
def test_a_push_deploys_staging_only_while_it_is_the_default_branch_s_newest_commit(
    tmp_path: Path, event: str, tip: str, deployed: bool
) -> None:
    env = {"EVENT": event, "REF": "master", "DEFAULT_BRANCH": "master", "SHORT": SHA[:8], "FAKE_LAST": "deployed",
           **_default_branch_at(tmp_path, "master", tip)}  # fmt: skip

    done = run_step("ci.yml", "deploy-staging", "Deploy staging", tmp_path, env)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "home" / "ssh-args").exists() is deployed


def test_only_the_default_branch_or_a_run_by_hand_builds_and_deploys() -> None:
    jobs = workflow("ci.yml")["jobs"]

    def upstream(name: str) -> set[str]:
        needs = jobs[name].get("needs", [])
        direct = {needs} if isinstance(needs, str) else set(needs)
        return direct.union(*(upstream(n) for n in direct))

    plan = "github.event_name == 'workflow_dispatch' || github.ref_name == github.event.repository.default_branch"
    assert jobs["plan"]["if"] == plan
    assert all("plan" in upstream(name) for name in jobs if name not in ("checks", "plan"))
    # A condition that runs past a skipped job must still demand a success upstream, or it runs when plan is skipped.
    for name, job in jobs.items():
        if "!failure()" in job.get("if", ""):
            assert re.search(r"needs\.[\w-]+\.result == 'success'", job["if"]), name


@pytest.mark.parametrize(
    ("event", "ref", "mode"),
    [
        ("push", "", "follow"),
        ("workflow_dispatch", "feature/login", "pin feature/login"),
        ("workflow_dispatch", "main", "pin -"),
    ],
)
def test_the_staging_deploy_follows_on_a_push_and_pins_a_run_by_hand(
    tmp_path: Path, event: str, ref: str, mode: str
) -> None:
    env = {"EVENT": event, "REF": ref, "DEFAULT_BRANCH": "main", "SHORT": SHA[:8], "FAKE_LAST": "",
           **_default_branch_at(tmp_path, "main", SHA)}  # fmt: skip

    done = run_step("ci.yml", "deploy-staging", "Deploy staging", tmp_path, env)

    server = settings.load(tmp_path).server
    assert done.returncode == 0, done.stderr
    args = (tmp_path / "home" / "ssh-args").read_text().splitlines()
    assert args[-2:] == [f"ci@{server.host}", f"deploy staging {SHA[:8]} {mode}"]
    assert "StrictHostKeyChecking=yes" in args
    assert (tmp_path / "home" / ".ssh" / "known_hosts").read_text() == f"{server.host} {server.host_key}\n"
    assert (tmp_path / "home" / "ssh-stdin").read_text() == "a registry token"


@pytest.mark.parametrize(
    ("last", "deployed"),
    [
        (f"deployed prod {SHA[:8]} previous 89abcdef", True),
        (f"deployed prod {SHA[:8]} previous -", True),
        ("deployed prod 99999999 previous 89abcdef", False),
        (f"deployed staging {SHA[:8]} previous 89abcdef", False),
    ],
)
def test_the_prod_deploy_passes_only_when_the_server_says_it_deployed_this_commit(
    tmp_path: Path, last: str, deployed: bool
) -> None:
    done = run_step("deploy-prod.yml", "deploy", "Deploy prod", tmp_path, {"SHA": SHA, "FAKE_LAST": last})

    args = (tmp_path / "home" / "ssh-args").read_text().splitlines()
    assert args[-1] == f"deploy prod {SHA[:8]}"
    assert (done.returncode == 0) is deployed, done.stderr
