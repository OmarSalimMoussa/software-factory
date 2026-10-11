"""The factory's own releases on GitHub, read with the owner's read-only token for the factory repo.

The warden reads them to offer an upgrade: the newest release, and what changed since the installed commit. Root's
upgrade service reads them again before it installs one, trusting nothing the warden said: the release is CI's
(tagged for its commit, after CI passed on a push to the default branch), it moves forward from the installed commit,
and its file matches the checksum published beside it. The token never appears in an error.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.shared.paths import Places

API = "https://api.github.com"
ASSET = "software-factory.pyz"
CHECKSUM = f"{ASSET}.sha256"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
CI_BOT = "github-actions[bot]"  # the account a workflow's own token acts as: the release job's
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# GitHub's compare lists at most this many commits; a longer changelog says how many it leaves out.
_COMPARE_MAX = 250


class ReleaseError(Exception):
    """A release could not be read or trusted; the message says which and why, never the token."""


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


# Any HTTP answer is an Answer, a redirect included (never followed); raises OSError when nothing answers.
Get = Callable[[str, dict[str, str], float], Answer]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return Answer(reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read())
    # telemetry: expected — an error status is an answer: the caller reads its status and body
    except urllib.error.HTTPError as exc:
        return Answer(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())


def token_path(places: Places) -> Path:
    return places.state / "secrets" / "factory.token"


def tag(commit: str) -> str:
    return f"build-{commit[:12]}"


@dataclass(frozen=True, slots=True)
class Release:
    commit: str
    tag: str
    published: str
    assets: dict[str, str]


class Releases:
    def __init__(self, repo: str, token: str, timeout: float, get: Get) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ReleaseError(f"factory.github must be owner/name, not {repo!r}")
        self._repo = f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        self._token, self._timeout, self._get = token, timeout, get

    @classmethod
    def of(cls, path: Path, repo: str, timeout: float, get: Get) -> Releases:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            why = f"the factory token at {path} cannot be read ({type(exc).__name__})"
            raise ReleaseError(f"{why}: upgrade once from the release file, which asks for it") from exc
        if not token or not token.isprintable():
            raise ReleaseError(f"the factory token at {path} is empty or not one line of text")
        return cls(repo, token, timeout, get)

    def default_branch(self) -> str:
        branch = self._json(f"/repos/{self._repo}").get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ReleaseError("GitHub names no default branch for the factory repo")
        return branch

    def can_read_runs(self) -> None:
        """Raises unless the token may read the factory's CI runs, which an upgrade checks."""
        self._json(f"/repos/{self._repo}/actions/runs?per_page=1")

    def latest(self) -> Release:
        return self._release(self._json(f"/repos/{self._repo}/releases/latest"))

    def of_commit(self, commit: str) -> Release:
        found = self._release(self._json(f"/repos/{self._repo}/releases/tags/{tag(commit)}"))
        pointed = self._json(f"/repos/{self._repo}/git/ref/tags/{tag(commit)}").get("object")
        if found.commit != commit or not isinstance(pointed, dict) or pointed.get("sha") != commit:
            raise ReleaseError(f"the tag {tag(commit)} does not point at {commit[:12]}")
        return found

    def _release(self, body: dict[str, Any]) -> Release:
        """A release CI's own token made: its tag names its commit, and CI uploaded each file to this repo."""
        commit, name, published = body.get("target_commitish"), body.get("tag_name"), body.get("published_at")
        assets, author = body.get("assets"), body.get("author")
        if not isinstance(commit, str) or not FULL_SHA.match(commit) or name != tag(commit):
            raise ReleaseError(f"the release {name} names no full commit its tag is made of")
        if not isinstance(published, str) or not isinstance(assets, list) or not _by_ci(author):
            raise ReleaseError(f"the release {name} is not one CI made")
        own = f"{API}/repos/{self._repo}/releases/assets/"
        files = {str(a.get("name")): str(a.get("url")) for a in assets if isinstance(a, dict)}
        if not all(isinstance(a, dict) and _by_ci(a.get("uploader")) for a in assets) or not all(
            url.lower().startswith(own.lower()) for url in files.values()
        ):
            raise ReleaseError(f"a file of the release {name} was not uploaded by CI to this repo")
        return Release(commit, name, published, files)

    def changes(self, base: str, head: str) -> tuple[str, list[str]]:
        """How ``head`` stands to ``base`` (ahead, behind...) and the subjects of the commits it adds, oldest first."""
        body = self._json(f"/repos/{self._repo}/compare/{base}...{head}")
        status = body.get("status")
        commits = body.get("commits")
        if not isinstance(status, str) or not isinstance(commits, list):
            raise ReleaseError(f"GitHub's comparison of {base[:12]} and {head[:12]} has no status or commits")
        subjects = [
            str(c.get("commit", {}).get("message", "")).split("\n", 1)[0] for c in commits if isinstance(c, dict)
        ]
        total = body.get("total_commits")
        if isinstance(total, int) and total > _COMPARE_MAX:
            subjects.append(f"…and {total - len(subjects)} more")
        return status, subjects

    def ci_passed(self, commit: str, branch: str) -> bool:
        wanted = {"head_sha": commit, "branch": branch, "event": "push", "status": "success", "per_page": 1}
        query = urllib.parse.urlencode(wanted)
        count = self._json(f"/repos/{self._repo}/actions/runs?{query}").get("total_count")
        return isinstance(count, int) and count > 0

    def download(self, url: str) -> bytes:
        """A release file's bytes. GitHub answers with a redirect to its storage, which is asked without the token."""
        answer = self._call(url, "application/octet-stream")
        if answer.status in (301, 302, 303, 307, 308):
            where = answer.headers.get("location", "")
            if not where.startswith("https://"):
                raise ReleaseError("GitHub sent the release file somewhere that is not https")
            try:
                answer = self._get(where, {}, self._timeout)
            except OSError as exc:
                raise ReleaseError(f"the release file's storage did not answer ({type(exc).__name__})") from exc
        if answer.status != 200:
            raise ReleaseError(f"the release file could not be downloaded: HTTP {answer.status}")
        return answer.body

    def _json(self, path: str) -> dict[str, Any]:
        answer = self._call(API + path, _HEADERS["Accept"])
        if answer.status >= 300:
            raise ReleaseError(f"GitHub answered GET {path.split('?')[0]} with HTTP {answer.status}")
        try:
            parsed = json.loads(answer.body)
        except ValueError as exc:
            raise ReleaseError(f"GitHub answered GET {path.split('?')[0]} with something that is not JSON") from exc
        if not isinstance(parsed, dict):
            raise ReleaseError(f"GitHub answered GET {path.split('?')[0]} with no object")
        return parsed

    def _call(self, url: str, accept: str) -> Answer:
        where = url.removeprefix(API).split("?")[0]
        headers = {**_HEADERS, "Accept": accept, "Authorization": f"Bearer {self._token}"}
        try:
            answer = self._get(url, headers, self._timeout)
        except OSError as exc:
            raise ReleaseError(f"GitHub did not answer GET {where} ({type(exc).__name__})") from exc
        if answer.status in (401, 403, 404):
            needs = "The factory token needs Contents: Read and Actions: Read on the factory repo"
            raise ReleaseError(f"GitHub refused GET {where}: HTTP {answer.status}. {needs}")
        if answer.status >= 400:
            raise ReleaseError(f"GitHub refused GET {where}: HTTP {answer.status}")
        return answer


def _by_ci(account: object) -> bool:
    return isinstance(account, dict) and account.get("login") == CI_BOT
