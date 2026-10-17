"""The warden's reachability probe of GitHub's API: the outage's control. It carries no token."""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from runtime.shared import network
from runtime.shared.network import Answer
from runtime.shared.settings import Settings

API = "https://api.github.com"

# Raises OSError when GitHub cannot be reached; any HTTP answer is an Answer.
Transport = Callable[[urllib.request.Request, float, bool], Answer]


def transport(request: urllib.request.Request, timeout: float, follow: bool) -> Answer:
    return network.call(request, timeout, follow=follow)


class RestGitHub:
    """runtime.warden.context.GitHub over the REST API; the timeout is read fresh on each call."""

    def __init__(self, settings: Callable[[], Settings], transport: Transport) -> None:
        self._settings = settings
        self._transport = transport

    def reachable(self) -> bool:
        """Whether GitHub's API answers at all. Any HTTP answer below 500 counts."""
        request = urllib.request.Request(f"{API}/rate_limit", headers=_HEADERS)
        timeout = float(self._settings().get_int("watch.http_timeout_seconds"))
        try:
            reply = self._transport(request, timeout, True)
        # telemetry: expected — GitHub not answering is the False that `reachable` asks about
        except (OSError, TimeoutError):
            return False
        return reply.status < 500


_HEADERS: dict[str, Any] = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
