"""A long action's steps (a push, a settings save, a change of repo, a pause) as the warden runs them; a finished one
says what it did and which parts of the page to reload."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import PartError

_TITLES = {
    "settings": "Saving settings",
    "repo": "Changing the repo",
    "push": "Pushing to prod",
    "push back": "Pushing prod back",
    "pause": "Pausing the factory",
}
_REFRESH = {
    "settings": ["settings", "shell"],
    "repo": ["settings", "shell"],
    "push": ["home", "shell"],
    "push back": ["home", "shell"],
    "pause": ["agents", "home", "shell"],
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
            "settings": f"Saved as {str(sha)[:7]} in the factory repo.",
            "repo": "The factory works on the new repo.",
            "push": f"Prod runs {str(sha)[:7]}.",
            "push back": f"Prod runs {str(sha)[:7]} again.",
            "pause": "Paused. Every agent is benched with its work kept.",
        }.get(op["kind"], "Done.")
    return {
        "id": op["id"],
        "title": _TITLES.get(op["kind"], "Working"),
        "state": op["state"],
        # The warden's details are clauses; the owner reads them as sentences.
        "steps": [{**step, "detail": step["detail"][:1].upper() + step["detail"][1:]} for step in op["steps"]],
        "result": result,
    }
