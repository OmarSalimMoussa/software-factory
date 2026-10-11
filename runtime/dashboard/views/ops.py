"""A long action's steps (a push, a settings save, a change of repo, a pause, a switch of Claude accounts) as the
warden runs them; a finished one says what it did and which parts of the page to reload."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import PartError

_TITLES = {
    "settings": "Saving settings",
    "repo": "Changing the repo",
    "push": "Pushing to prod",
    "push back": "Pushing prod back",
    "pause": "Pausing the factory",
    "account": "Switching the Claude account",
}
_REFRESH = {
    "settings": ["settings", "shell"],
    "repo": ["settings", "shell"],
    "push": ["home", "shell"],
    "push back": ["home", "shell"],
    "pause": ["agents", "home", "shell"],
    "account": ["settings", "agents", "shell"],
}

# How the sheet draws a step in each state the warden reports: a class for its row, and a mark that is an icon or the
# page's own spinner or empty ring.
_STEP_LOOK = {
    "waiting": {"cls": "", "mark": "ring"},
    "running": {"cls": "on", "mark": "spin"},
    "done": {"cls": "done", "mark": "check"},
    "failed": {"cls": "failed", "mark": "x-mark"},
}


def operation(answer: dict[str, Any]) -> dict[str, Any]:
    """The op.get answer as the sheet shows it; a finished one says what it did and which parts to reload."""
    op = answer.get("op")
    if not isinstance(op, dict):
        raise PartError("The warden sent no operation.")
    result: dict[str, Any] = {"refresh": _REFRESH.get(op["kind"], ["all"])}
    sha = op["result"].get("commit") or op["result"].get("sha")
    if op["state"] == "done":
        result["message"] = {
            "settings": "Saved. The new values are in effect.",
            "repo": "The factory works on the new repo.",
            "push": f"Prod runs {str(sha)[:7]}.",
            "push back": f"Prod runs {str(sha)[:7]} again.",
            "pause": "Paused. Every agent is benched with its work kept.",
            "account": f"The agents now use {op['result'].get('name')}.",
        }.get(op["kind"], "Done.")
    return {
        "id": op["id"],
        "title": _TITLES.get(op["kind"], "Working"),
        "state": op["state"],
        # The warden's details are clauses; the owner reads them as sentences.
        "steps": [
            {**step, "detail": step["detail"][:1].upper() + step["detail"][1:], "look": _STEP_LOOK[step["state"]]}
            for step in op["steps"]
        ],
        "result": result,
    }
