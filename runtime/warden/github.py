"""The warden's door to GitHub's REST API: the product's prod workflow and its runs, and a reachability probe that
serves as the outage's control.

The token is read from the warden's secrets on every call and never appears in an error or a log line. Every
failure raises GitHubError with one line that says what failed; a rate limit says when it resets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared.paths import CI_WORKFLOW, PROMOTE_WORKFLOW, Places
from runtime.shared.settings import Settings

API = "https://api.github.com"


class GitHubError(Exception):
    """A GitHub call failed; the message says which and why, never the token."""


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes


# Raises OSError (URLError included) or TimeoutError when GitHub cannot be reached; any HTTP answer is a Reply.
Transport = Callable[[urllib.request.Request, float], Reply]


def urlopen_transport(request: urllib.request.Request, timeout: float) -> Reply:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- a fixed https API
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())


def token_path(places: Places) -> Path:
    return places.state / "secrets" / "github.token"


class RestGitHub:
    """runtime.warden.context.GitHub over the REST API; the repo, branch and timeout are read fresh on each call."""

    def __init__(self, places: Places, settings: Callable[[], Settings], transport: Transport) -> None:
        self._token_file = token_path(places)
        self._settings = settings
        self._transport = transport

    def dispatch_promote(self, sha: str) -> str:
        """Start the product's prod workflow for ``sha`` on the default branch; the id of the run it started."""
        branch = self._settings().get_str("repo.default_branch")
        body = {"ref": branch, "inputs": {"sha": sha}, "return_run_details": True}
        reply = self._json("POST", f"/repos/{self._repo()}/actions/workflows/{PROMOTE_WORKFLOW}/dispatches", body)
        run_id = reply.get("workflow_run_id")
        if not isinstance(run_id, int):
            raise GitHubError("GitHub started the push workflow but did not name its run")
        return str(run_id)

    def full_sha(self, sha: str) -> str:
        """The full commit a short one names on GitHub; products may report short builds, a workflow needs the full."""
        body = self._json("GET", f"/repos/{self._repo()}/commits/{_seg(sha)}")
        full = body.get("sha")
        if not isinstance(full, str) or len(full) != 40:
            raise GitHubError(f"GitHub names no full commit for {sha}")
        return full

    def ci_run(self, sha: str) -> str | None:
        """The newest run of the product's CI on a push of ``sha``; None until GitHub has started one."""
        query = urllib.parse.urlencode({"head_sha": sha, "event": "push", "per_page": 1})
        body = self._json("GET", f"/repos/{self._repo()}/actions/workflows/{CI_WORKFLOW}/runs?{query}")
        runs = body.get("workflow_runs")
        if not isinstance(runs, list):
            raise GitHubError("GitHub's list of CI runs is not a list")
        found = runs[0].get("id") if runs and isinstance(runs[0], dict) else None
        return str(found) if isinstance(found, int) else None

    def run(self, run_id: str) -> dict[str, Any]:
        """A workflow run: id, status (queued, waiting, in_progress, completed), conclusion, url, head_sha, and
        jobs: each job's status and conclusion by its name."""
        body = self._json("GET", f"/repos/{self._repo()}/actions/runs/{_seg(run_id)}")
        status = body.get("status")
        if not isinstance(status, str):
            raise GitHubError(f"GitHub's run {run_id} has no status")
        listed = self._json("GET", f"/repos/{self._repo()}/actions/runs/{_seg(run_id)}/jobs").get("jobs")
        if not isinstance(listed, list):
            raise GitHubError(f"GitHub's run {run_id} has no list of jobs")
        jobs = {
            str(job.get("name")): {"status": job.get("status"), "conclusion": job.get("conclusion")}
            for job in listed
            if isinstance(job, dict)
        }
        return {
            "id": str(body.get("id")),
            "status": status,
            "conclusion": body.get("conclusion"),
            "url": body.get("html_url"),
            "head_sha": body.get("head_sha"),
            "jobs": jobs,
        }

    def reachable(self) -> bool:
        """Whether GitHub's API answers at all: the control probe for an outage. Any HTTP answer below 500 counts."""
        request = urllib.request.Request(f"{API}/rate_limit", headers=_HEADERS)
        try:
            reply = self._transport(request, self._timeout())
        except (OSError, TimeoutError):
            return False
        return reply.status < 500

    def _repo(self) -> str:
        repo = self._settings().get_str("repo.github")
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise GitHubError(f"repo.github must be owner/name, not {repo!r}")
        return f"{_seg(owner)}/{_seg(name)}"

    def _timeout(self) -> float:
        return float(self._settings().get_int("watch.http_timeout_seconds"))

    def _token(self) -> str:
        try:
            token = self._token_file.read_text().strip()
        except OSError as exc:
            raise GitHubError(f"cannot read the GitHub token at {self._token_file}: {type(exc).__name__}") from exc
        if not token:
            raise GitHubError(f"the GitHub token at {self._token_file} is empty")
        return token

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        parsed = self._call(method, path, body)
        if not isinstance(parsed, dict):
            raise GitHubError(f"GitHub answered {method} {path.split('?')[0]} with no object")
        return parsed

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        where = f"{method} {path.split('?')[0]}"
        headers = {**_HEADERS, "Authorization": f"Bearer {self._token()}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            reply = self._transport(request, self._timeout())
        except (OSError, TimeoutError) as exc:
            raise GitHubError(f"GitHub did not answer {where}: {type(exc).__name__}") from exc
        if reply.status in (403, 429) and (reply.headers.get("x-ratelimit-remaining") == "0"
                                           or "retry-after" in reply.headers):  # fmt: skip
            raise GitHubError(f"GitHub's rate limit stops {where} until {_reset(reply.headers)}")
        if reply.status >= 400:
            raise GitHubError(f"GitHub refused {where}: HTTP {reply.status} {_message(reply.body)}".rstrip())
        if reply.status == 204 or not reply.body:
            return None
        try:
            return json.loads(reply.body)
        except ValueError as exc:
            raise GitHubError(f"GitHub answered {where} with something that is not JSON") from exc


_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _seg(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _message(body: bytes) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    message = parsed.get("message") if isinstance(parsed, dict) else None
    return " ".join(str(message).split())[:200] if message else ""


def _reset(headers: dict[str, str]) -> str:
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return datetime.fromtimestamp(int(reset), UTC).strftime("%H:%M UTC")
    retry = headers.get("retry-after", "")
    return f"{retry} seconds from now" if retry.isdigit() else "an unknown time"
