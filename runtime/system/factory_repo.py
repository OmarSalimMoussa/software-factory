"""The factory's own repo on GitHub, read with the owner's read-only token: whether the token reads it, and one
commit's files as GitHub's tarball, which root's upgrade unit installs. The token goes to GitHub's API alone and never
appears in an error.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from runtime.shared import network
from runtime.shared.network import Answer

API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class RepoError(Exception):
    """The factory repo could not be read; the message says which call failed and why, never the token."""


# Any HTTP answer is an Answer, a redirect included (never followed); raises OSError when nothing answers.
Get = Callable[[str, dict[str, str], float], Answer]


def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
    return network.call(urllib.request.Request(url, headers=headers), timeout)


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RepoError(f"the factory token at {path} cannot be read ({type(exc).__name__}): install by hand once, "
                        "which asks for it") from exc  # fmt: skip
    if not token or not token.isprintable():
        raise RepoError(f"the factory token at {path} is empty or not one line of text")
    return token


class FactoryRepo:
    def __init__(self, repo: str, token: str, timeout: float, get: Get) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise RepoError(f"the factory repo must be owner/name, not {repo!r}")
        self._path = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        self._token, self._timeout, self._get = token, timeout, get

    def check(self) -> None:
        """Raises RepoError unless the token reads the repo."""
        if self._call(self._path).status != 200:
            raise RepoError(f"GitHub sent no answer to read for {self._path}")

    def tarball(self, sha: str) -> bytes:
        """The files of ``sha`` as GitHub archives them. GitHub answers with a redirect to its storage, which is asked
        without the token."""
        answer = self._call(f"{self._path}/tarball/{sha}")
        where = answer.headers.get("location", "")
        if answer.status not in _REDIRECTS or not where.startswith("https://"):
            raise RepoError(f"GitHub sent no https address for the tarball of {sha[:12]} (HTTP {answer.status})")
        try:
            stored = self._get(where, {}, self._timeout)
        except OSError as exc:
            raise RepoError(f"GitHub's storage did not answer ({network.failure_class(exc)})") from exc
        if stored.status != 200:
            raise RepoError(f"the tarball of {sha[:12]} could not be downloaded: HTTP {stored.status}")
        return stored.body

    def _call(self, path: str) -> Answer:
        headers = {**_HEADERS, "Authorization": f"Bearer {self._token}"}
        try:
            answer = self._get(API + path, headers, self._timeout)
        except OSError as exc:
            raise RepoError(f"GitHub did not answer GET {path} ({network.failure_class(exc)})") from exc
        if answer.status in (401, 403, 404):
            raise RepoError(f"GitHub refused GET {path}: HTTP {answer.status}. The factory token needs Contents: Read "
                            "on the factory repo")  # fmt: skip
        if answer.status >= 400:
            raise RepoError(f"GitHub refused GET {path}: HTTP {answer.status}")
        return answer
