"""What each session starts with: its own settings file (--settings) and its agent definitions (--agents).

The settings file outranks the project's and the user's. It forces the sandbox on and walls the session into its
worktree: shell commands write only there and in their tools' caches in the agent's home, read no other worktree
and not Claude Code's sign-in token, and reach only the allowed hosts. The one command let out of the sandbox is the
factory's own, so it can reach the warden. The orchestrator's file also runs the status line that records the plan's
limits.

The agent definitions come from the rendered plugin's launchers, passed as JSON because Claude Code ignores a plugin
agent's mcpServers and initialPrompt (the browser testers need their browser server). A session gets its own
launcher and those of the subagent kinds its role may start.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

ORCHESTRATOR = "orchestrator"
_FIELD = re.compile(r"^([A-Za-z][\w-]*):\s*(.*)$")
_LISTS = ("tools", "disallowedTools")  # a comma-separated line in a launcher, an array in --agents


def worktrees(repo: Path) -> Path:
    """Where `claude --worktree <name>` puts each session's worktree."""
    return repo / ".claude" / "worktrees"


def render(settings: Settings, places: Places, role: str, worktree: Path, agent_home: Path) -> dict[str, Any]:
    repo = Path(settings.get_str("repo.path"))
    others = worktrees(repo)
    credentials = agent_home / ".claude" / ".credentials.json"
    caches = [str(agent_home / cache) for cache in settings.get_list("sandbox.writable_caches")]
    filesystem: dict[str, list[str]] = {
        "allowWrite": [str(worktree), *caches],
        "denyRead": [str(others), str(credentials)],
    }
    if role == ORCHESTRATOR:
        # Its worktree is the main checkout, which holds every session's worktree: none of them is its to touch.
        filesystem["denyWrite"] = [str(others)]
    else:
        # Re-opens its own worktree inside the folder of worktrees denied above.
        filesystem["allowRead"] = [str(worktree)]
    body: dict[str, Any] = {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # Matched against the call's text: only a call that is `factory` alone, no redirect or pipe, runs outside.
            "excludedCommands": ["factory", "factory *"],
            "network": {"allowedDomains": settings.get_list("sandbox.allowed_domains")},
            "filesystem": filesystem,
        },
        "permissions": {"deny": [f"Read(/{credentials})"]},  # "//" starts an absolute path in a permission rule
        "env": {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"},
    }
    if role == ORCHESTRATOR:
        script = places.plugin / "hooks" / "statusline.py"
        body["statusLine"] = {"type": "command", "command": f"{sys.executable} -I {script}"}
    return body


def write(settings: Settings, places: Places, request_id: str, role: str, worktree: Path, agent_home: Path) -> Path:
    """Write the session's settings where the agent user can read them; the path to pass with --settings."""
    path = places.state / "public" / "session-settings" / f"{request_id}.json"
    store.write_json(path, render(settings, places, role, worktree, agent_home))
    return path


def _value(text: str) -> Any:
    # A quoted string or a [flow, list] in a launcher is also JSON.
    if text.startswith(('"', "[")):
        return json.loads(text)
    return int(text) if text.isdigit() else text


def read_launcher(path: Path) -> tuple[str, dict[str, Any]]:
    """One launcher as --agents takes it: its name, and its front matter's fields with its body as the prompt.

    Launchers use a small part of YAML: plain or quoted values, lists of plain items, and mcpServers' list of
    one-key maps. Anything else raises ValueError naming the file and line.
    """
    lines = path.read_text().split("\n")
    heads = [n for n, line in enumerate(lines) if line.strip() == "---"]
    if len(heads) < 2 or heads[0] != 0:
        raise ValueError(f"{path}: an agent launcher opens with front matter between two --- lines")
    fields: dict[str, Any] = {}
    key = ""
    for n, line in enumerate(lines[1 : heads[1]], 2):
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        found = _FIELD.match(item)
        if not line.startswith(" ") and found:
            key = found.group(1)
            fields[key] = _value(found.group(2)) if found.group(2) else []
        elif isinstance(fields.get(key), list) and item.startswith("- "):
            entry = item[2:].strip()
            fields[key].append({entry[:-1]: {}} if entry.endswith(":") else _value(entry))
        elif found and isinstance(fields.get(key), list) and fields[key] and isinstance(fields[key][-1], dict):
            next(iter(fields[key][-1].values()))[found.group(1)] = _value(found.group(2))
        else:
            raise ValueError(f"{path}:{n}: a launcher line this reader does not know: {item[:60]}")
    for name in _LISTS:
        if isinstance(fields.get(name), str):
            fields[name] = [tool.strip() for tool in fields[name].split(",") if tool.strip()]
    name = fields.pop("name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: the launcher names no agent")
    return name, {**fields, "prompt": "\n".join(lines[heads[1] + 1 :]).strip()}


def agents(settings: Settings, places: Places, role: str, model: str, effort: str) -> dict[str, Any]:
    """The --agents definitions for a session of ``role``, which runs on ``model`` at ``effort``.

    A kind without a launcher is one of Claude Code's own (Explore) and needs none. A launcher whose model or effort
    was not rendered from the settings raises ValueError.
    """
    folder = places.rendered_plugin / "agents"
    found: dict[str, Any] = {}
    for kind in [role, *settings.get_list(f"subagents.{role}")]:
        path = folder / f"{kind}.md"
        if kind != role and not path.exists():
            continue
        name, definition = read_launcher(path)
        if kind == role:
            definition.update(model=model, effort=effort)
        unrendered = [k for k in ("model", "effort") if "{{" in str(definition.get(k, ""))]
        if unrendered:
            raise ValueError(f"{path}: {', '.join(unrendered)} not rendered from the settings")
        found[name] = definition
    return found
