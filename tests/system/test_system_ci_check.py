from __future__ import annotations

from pathlib import Path

import pytest
from system_fakes import FACTORY_ORIGIN, GH, PASSING, SHA, ci_answers, ci_link, dry_job

from runtime.shared.protocol import Refused
from runtime.system import ci_check

SHORT = SHA[:12]


@pytest.mark.parametrize(
    ("runs", "signed_in", "origin", "refusal"),
    [
        ([*PASSING, ("lint", "completed", "skipped"), ("docs", "completed", "neutral")], True, FACTORY_ORIGIN, None),
        ([("test", "completed", "failure"), ("drives", "in_progress", "")], True, FACTORY_ORIGIN,
         [f"the factory's CI failed on {SHORT}:", f"  test: failed (failure): {ci_link(0)}",
          f"  drives: still running: {ci_link(1)}", ci_check.AGAIN]),
        ([("test", "completed", "success"), ("drives", "queued", "")], True, FACTORY_ORIGIN,
         [f"the factory's CI is still running on {SHORT}:", f"  drives: still running: {ci_link(1)}", ci_check.AGAIN]),
        ([], True, FACTORY_ORIGIN,
         [f"GitHub holds no CI run for {SHORT}: push it and wait for the factory's CI to pass on it"]),
        (PASSING, False, FACTORY_ORIGIN, [ci_check.NO_GH]),
        (PASSING, True, "https://git.example/acme/factory.git",
         ["the factory's origin (https://git.example/acme/factory.git) is not on GitHub, so its CI cannot be checked: "
          "an install takes only a commit the factory's CI passed"]),
    ],
    ids=["all-passed", "one-failed", "one-running", "none", "no-gh", "no-github-origin"],
)  # fmt: skip
def test_an_install_takes_only_a_commit_the_factorys_ci_passed(
    tmp_path: Path, runs: list[tuple[str, str, str]], signed_in: bool, origin: str, refusal: list[str] | None
) -> None:
    job = dry_job(tmp_path)
    job.host.dry = False  # decides like a real install; a dry host still changes nothing
    job.sha, job.factory_origin = SHA, origin
    job.host.answers.update(ci_answers(job.factory, SHA, runs))
    if not signed_in:
        job.host.answers[f"{GH} auth status --hostname github.com"] = (1, "You are not logged into any GitHub hosts.")

    if refusal is None:
        ci_check.passed(job)
        return
    with pytest.raises(Refused) as refused:
        ci_check.passed(job)

    assert refused.value.reason.splitlines() == refusal
