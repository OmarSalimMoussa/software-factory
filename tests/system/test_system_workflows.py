from __future__ import annotations

from pathlib import Path

from standard import yaml_subset

FLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_the_factorys_own_ci_runs_everything_an_install_trusts() -> None:
    text = (FLOWS / "ci.yml").read_text(encoding="utf-8")

    for command in ("pytest", "ruff check", "ruff format --check", "mypy"):
        assert f"--extra dev {command}" in text
    assert "node tests/dashboard_ui/run.cjs" in text  # the browser drives, which pytest skips without a browser


def test_a_release_is_published_only_from_the_default_branch_after_every_check_passed() -> None:
    flow = yaml_subset.load((FLOWS / "ci.yml").read_text(encoding="utf-8"))
    jobs = flow["jobs"]
    release = jobs["release"]

    assert set(release["needs"]) == set(jobs) - {"release"}
    assert release["if"] == "github.event_name == 'push' && github.ref_name == github.event.repository.default_branch"
    # Only the job that publishes may write; every check reads.
    assert flow["permissions"] == {"contents": "read"} and release["permissions"] == {"contents": "write"}
    assert all("permissions" not in job for name, job in jobs.items() if name != "release")
    published = " ".join(step.get("run", "") for step in release["steps"])
    assert "--check" in published and "gh release create" in published and "--latest" in published
