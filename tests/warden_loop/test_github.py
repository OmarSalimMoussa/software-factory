"""The warden's GitHub probe: any answer below 500 means GitHub is up, and the probe carries no token."""

from __future__ import annotations

import urllib.request

import pytest
from loop_fakes import World

from runtime.shared.network import Answer
from runtime.warden.delivery import github


@pytest.mark.parametrize(
    ("answer", "up"),
    [(Answer(200, {}, b"{}"), True), (Answer(404, {}, b""), True), (Answer(503, {}, b""), False),
     (OSError("refused"), False)],
    ids=["answers", "refuses-the-path", "server-error", "no-answer"],
)  # fmt: skip
def test_github_is_reachable_on_any_answer_below_500_and_the_probe_carries_no_token(
    world: World, answer: Answer | OSError, up: bool
) -> None:
    sent: list[urllib.request.Request] = []

    def transport(request: urllib.request.Request, timeout: float, follow: bool) -> Answer:
        sent.append(request)
        if isinstance(answer, OSError):
            raise answer
        return answer

    assert github.RestGitHub(world.ctx.settings, transport).reachable() is up
    [asked] = sent
    assert asked.full_url == f"{github.API}/rate_limit" and not asked.has_header("Authorization")
