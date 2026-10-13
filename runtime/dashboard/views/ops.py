"""A long action's steps (a push, a settings save, a change of repo, a pause, a switch of Claude accounts) as the
warden runs them; a finished one says what it did and which parts of the page to reload."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.dashboard.views.kit import PartError
from runtime.shared import operations as kinds

Said = Callable[[dict[str, Any]], str]
# Each kind's title, the parts of the page it changes, and what it says done, from what it recorded.
_KINDS: dict[str, tuple[str, list[str], Said]] = {
    kinds.SETTINGS: ("Saving settings", ["settings", "shell"], lambda r: "Saved. The new values are in effect."),
    kinds.REPO: ("Changing the repo", ["settings", "shell"], lambda r: "The factory works on the new repo."),
    kinds.PUSH: ("Pushing to prod", ["home", "shell"], lambda r: f"Prod runs {r['sha'][:7]}."),
    kinds.PUSH_BACK: ("Pushing prod back", ["home", "shell"], lambda r: f"Prod runs {r['sha'][:7]} again."),
    kinds.STAGING_PUSH: (
        "Pushing to staging",
        ["home", "shell"],
        lambda r: f"Pushed. CI deploys {r['sha'][:7]} to staging.",
    ),
    kinds.PAUSE: (
        "Pausing the factory",
        ["agents", "home", "shell"],
        lambda r: "Paused. Every agent is benched with its work kept.",
    ),
    kinds.RESUME: (
        "Resuming the factory",
        ["agents", "home", "shell"],
        lambda r: "Resumed. Every benched agent is back at work.",
    ),
    kinds.ACCOUNT: (
        "Switching the Claude account",
        ["settings", "agents", "shell"],
        lambda r: f"The agents now use {r['name']}.",
    ),
    kinds.MESSAGE: (
        "Sending your message",
        ["agent", "home"],
        lambda r: "Sent. It is back from the bench and reads your message now."
        if r["revived"]
        else "Sent. It reads your message as soon as its current turn ends.",
    ),
    kinds.WORD: (
        "Telling the agent",
        ["agent", "home"],
        lambda r: "Told. It reads it as soon as its current turn ends.",
    ),
    kinds.RELEASE_CHECK: (
        "Checking for a newer factory",
        ["settings", "home", "shell"],
        lambda r: "A newer factory is ready: Home has the upgrade." if r["newer"] else "This is the newest factory.",
    ),
    kinds.UPGRADE: (
        "Getting ready to upgrade",
        ["home", "shell"],
        lambda r: "The upgrade has started. The dashboard comes back when it is done.",
    ),
}

# The sheet's row class and mark for a step in each state: an icon, or the page's own spinner or empty ring.
_STEP_LOOK = {
    "waiting": {"cls": "", "mark": "ring"},
    "running": {"cls": "on", "mark": "spin"},
    "done": {"cls": "done", "mark": "check"},
    "failed": {"cls": "failed", "mark": "x-mark"},
}


def _kind(kind: str) -> tuple[str, list[str], Said]:
    if kind not in _KINDS:
        raise PartError(f"The warden sent an operation the dashboard does not know: {kind}.")
    return _KINDS[kind]


def operation(answer: dict[str, Any]) -> dict[str, Any]:
    """The op.get answer as the sheet shows it; a finished one says what it did and which parts to reload."""
    op = answer.get("op")
    if not isinstance(op, dict):
        raise PartError("The warden sent no operation.")
    heading, refresh, said = _kind(op["kind"])
    result: dict[str, Any] = {"refresh": refresh}
    if op["state"] == "done":
        result["message"] = said(op["result"])
    return {
        "id": op["id"],
        "title": heading,
        "state": op["state"],
        # The warden's details are clauses; the owner reads them as sentences.
        "steps": [
            {**step, "detail": step["detail"][:1].upper() + step["detail"][1:], "look": _STEP_LOOK[step["state"]]}
            for step in op["steps"]
        ],
        "result": result,
    }
