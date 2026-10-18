"""What each session starts with: its own settings file (--settings) and its agent definitions (--agents).

The settings file outranks the project's and the user's. A session runs with no sandbox, every tool allowed and no
permission prompt, and no auto-memory: the guard is its one wall. Claude Code honours bypass mode in a background
session only with consent in the managed settings, which the install writes. The orchestrator's file also runs the
status line that records the plan's limits.

The agent definitions come from the plugin's rendered launchers, passed as JSON because Claude Code ignores a plugin
agent's mcpServers and initialPrompt (the browser testers need their browser server). A session gets its own
launcher and those of the subagent kinds its role may start. They are fixed for the life of its process, and a
respawn keeps them: the registry holds their fingerprint, so a revive can tell when they differ from today's.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from runtime.shared import lanes, store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

ORCHESTRATOR = "orchestrator"
_FIELD = re.compile(r"^([A-Za-z][\w-]*):\s*(.*)$")
_LISTS = ("tools", "disallowedTools")  # a comma-separated line in a launcher, an array in --agents


def worktrees(repo: Path) -> Path:
    """Where `claude --worktree <name>` puts each session's worktree."""
    return repo / ".claude" / "worktrees"


def staging_at(repo: Path) -> Path:
    """The worktree slot 0 builds from while a check of the default branch walks a commit older than its tip; no
    session's worktree takes its name."""
    return worktrees(repo) / "staging-at"


def render(places: Places, role: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sandbox": {"enabled": False},
        "permissions": {"defaultMode": "bypassPermissions"},
        "autoMemoryEnabled": False,
        # Claude Code's own isolation would refuse the orchestrator every write in the main checkout, its .factory/
        # too; the guard walls each session in. A misspelt value is dropped unread: a test pins it.
        "worktree": {"bgIsolation": "none"},
    }
    if role == ORCHESTRATOR:
        script = places.plugin / "hooks" / "statusline.py"
        body["statusLine"] = {"type": "command", "command": f"{sys.executable} -I {script}"}
    return body


def folder(places: Places) -> Path:
    """Where sessions' settings go, in a folder the install makes the warden's."""
    return places.state / "public" / "session-settings"


def write(places: Places, request_id: str, role: str) -> Path:
    """Write the session's settings where the agent user can read them; the path to pass with --settings."""
    path = folder(places) / f"{request_id}.json"
    store.write_json(path, render(places, role))
    return path


def _value(text: str) -> Any:
    # A quoted string or a [flow, list] in a launcher is also JSON.
    if text.startswith(('"', "[")):
        return json.loads(text)
    return int(text) if text.isdigit() else text


def read_launcher(path: Path) -> tuple[str, dict[str, Any]]:
    """One launcher as --agents takes it: its name, and its front matter's fields with its body as the prompt.

    Launchers use a small part of YAML: plain or quoted values, lists of plain items, and mcpServers' list of
    one-key maps and names. Anything else raises ValueError naming the file and line.
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
    folder = places.rendered_agents
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
        found |= _lanes(settings, path, name, definition)
    return found


def fingerprint(definitions: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


def kinds(definitions: dict[str, Any], role: str) -> list[str]:
    return sorted(set(definitions) - {role})


def _lanes(settings: Settings, path: Path, kind: str, definition: dict[str, Any]) -> dict[str, Any]:
    """A browser tester's lanes after the first: its definition, starting its browser under the lane's name."""
    extra = lanes.names(settings, kind)[1:]
    servers = definition.get("mcpServers", [])
    at = [n for n, server in enumerate(servers) if isinstance(server, dict) and lanes.BROWSER in server]
    if extra and (len(at) != 1 or servers[at[0]][lanes.BROWSER].get("args") != [lanes.BROWSER, kind]):
        raise ValueError(f'{path}: a browser tester starts one {lanes.BROWSER} with ["{lanes.BROWSER}", "{kind}"]')
    found: dict[str, Any] = {}
    for lane in extra:
        copy = json.loads(json.dumps(definition))
        copy["mcpServers"][at[0]][lanes.BROWSER]["args"] = [lanes.BROWSER, lane]
        found[lane] = copy
    return found
