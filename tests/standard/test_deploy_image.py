"""The deploy image carries exactly the plan's files, the plan and the commit, and never a link or a gap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standard import deploy_image

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def repo_with_plan(tmp_path: Path, files: list[str]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    for rel in ("compose.yml", "conf/a.toml", "conf/deep/b.toml", "factory.toml", "secret.env", "infra/after.sh"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"{rel}\n")
    (repo / "infra" / "after.sh").chmod(0o755)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"commit": COMMIT, "files": files}))
    return repo, plan


def test_the_deploy_image_holds_exactly_the_plans_files(tmp_path: Path) -> None:
    repo, plan = repo_with_plan(tmp_path, ["compose.yml", "conf", "infra/after.sh", "factory.toml"])
    out = tmp_path / "context"
    assert deploy_image.main(["--repo", str(repo), "--plan", str(plan), "--out", str(out)]) == 0
    staged = sorted(str(p.relative_to(out / "deploy")) for p in (out / "deploy").rglob("*") if p.is_file())
    assert staged == ["COMMIT", "compose.yml", "conf/a.toml", "conf/deep/b.toml", "factory.toml", "infra/after.sh",
                      "plan.json"]  # fmt: skip
    assert sorted(p.name for p in out.iterdir()) == ["Dockerfile", "deploy"]
    assert (out / "deploy" / "COMMIT").read_text() == COMMIT + "\n"
    assert (out / "deploy" / "plan.json").read_bytes() == plan.read_bytes()
    assert (out / "deploy" / "infra" / "after.sh").stat().st_mode & 0o111
    assert (out / "Dockerfile").read_text() == "FROM scratch\nCOPY deploy/ /deploy/\n"


def test_a_missing_file_or_a_link_stops_the_staging(tmp_path: Path) -> None:
    repo, plan = repo_with_plan(tmp_path, ["compose.yml", "Caddyfile", "conf"])
    (repo / "conf" / "out.toml").symlink_to(repo / "secret.env")
    with pytest.raises(deploy_image.StageError) as refused:
        deploy_image.stage(repo, plan, tmp_path / "context")
    assert refused.value.problems == [
        "Caddyfile: the plan names it but the repo has no such file or folder",
        "conf/out.toml: is a link; deploy files must be real files",
    ]
    assert not (tmp_path / "context").exists()
