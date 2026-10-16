"""The warden's GitHub client: the token goes to GitHub's API alone, never to where GitHub keeps a job's log, whose
signed address never appears in an error either."""

from __future__ import annotations

import json
import re
import urllib.request

import pytest
from loop_fakes import World

from runtime.shared.network import Answer
from runtime.warden.delivery import github

STORE = "https://results.test/job-72.txt?sig=SIGNED"


def _client(world: World, *answers: Answer) -> tuple[github.RestGitHub, list[tuple[urllib.request.Request, bool]]]:
    token = github.token_path(world.ctx.places)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok-123\n")
    sent: list[tuple[urllib.request.Request, bool]] = []
    left = list(answers)

    def transport(request: urllib.request.Request, timeout: float, follow: bool, limit: int | None) -> Answer:
        sent.append((request, follow))
        return left.pop(0)

    return github.RestGitHub(world.ctx.places, world.ctx.settings, transport), sent


def test_a_job_log_is_read_from_where_github_keeps_it_without_the_token(world: World) -> None:
    client, sent = _client(world, Answer(302, {"location": STORE}, b""), Answer(200, {}, b"one\ntwo\n"))

    assert client.job_log("72", 1024) == "one\ntwo\n"

    (asked, asked_follows), (fetched, _) = sent
    assert asked.full_url.endswith("/repos/owner/name/actions/jobs/72/logs") and not asked_follows
    assert asked.get_header("Authorization") == "Bearer tok-123"
    assert fetched.full_url == STORE and not fetched.has_header("Authorization")


@pytest.mark.parametrize(
    ("answers", "said"),
    [
        ((Answer(200, {}, b"{}"),), "GitHub named no address for job 72's log (HTTP 200)"),
        ((Answer(302, {"location": "http://results.test/job-72.txt"}, b""),), "named no address"),
        ((Answer(302, {"location": STORE}, b""), Answer(403, {}, b"denied")), "refused it: HTTP 403"),
        ((Answer(302, {"location": STORE}, b""), Answer(200, {}, b"x" * 1025)), "over 1024 bytes"),
        ((Answer(410, {}, b'{"message": "Gone"}'),), "HTTP 410 Gone"),
    ],
    ids=["no-redirect", "plain-http", "store-refuses", "too-long", "expired"],
)
def test_a_job_log_that_cannot_be_read_says_why_and_never_names_the_signed_address_or_token(
    world: World, answers: tuple[Answer, ...], said: str
) -> None:
    client, _ = _client(world, *answers)

    with pytest.raises(github.GitHubError, match=re.escape(said)) as raised:
        client.job_log("72", 1024)

    assert "SIGNED" not in str(raised.value) and "tok-123" not in str(raised.value)


@pytest.mark.parametrize(
    ("moved", "found"),
    [
        ("https://api.github.com/repositories/9/commits/abc", "a" * 40),  # a renamed repository, followed once
        ("https://elsewhere.test/commits/abc", None),
        ("http://api.github.com/repositories/9/commits/abc", None),
    ],
    ids=["moved-repo", "other-host", "plain-http"],
)
def test_the_token_follows_a_redirect_only_within_githubs_api(world: World, moved: str, found: str | None) -> None:
    answer = Answer(200, {}, json.dumps({"sha": "a" * 40}).encode())
    client, sent = _client(world, Answer(301, {"location": moved}, b""), answer)

    if found is None:
        with pytest.raises(github.GitHubError, match="away from its API"):
            client.full_sha("abc")
    else:
        assert client.full_sha("abc") == found

    assert len(sent) == (2 if found else 1)
    assert all(request.full_url.startswith(f"{github.API}/") for request, _ in sent)
    assert all(request.get_header("Authorization") == "Bearer tok-123" and not follows for request, follows in sent)
