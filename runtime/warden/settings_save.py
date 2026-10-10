"""The owner's coded changes to the factory's tuning: settings, the product repo, and the guard's rule switches.

A save is checked whole before anything is written; a value that fails is refused with every reason. Then, as one
operation the dashboard follows: the overrides are written to the warden's state and take effect at once; whatever
reads the changed values is refreshed (the published settings, the subagents' launchers for a model or effort, the
slice for a budget); and settings.toml in the warden's own clone of the factory repo is committed and pushed, so
the repo stays the record. Every save and switch is audited.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import shutil
import subprocess
import threading
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.shared.paths import FACTORY_BIN, SLICE, Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings, SettingsError
from runtime.warden import ops, public, registry
from runtime.warden.context import Context
from runtime.warden.verbs import OWNER

log = logging.getLogger("factory.warden")

# Set by the installer; the dashboard shows them read-only.
READ_ONLY = frozenset({"models.allowed", "prices", "factory.clone"})
REPO_KEYS = ("repo.path", "repo.default_branch", "repo.github", "repo.staging_health", "repo.prod_health",
             "repo.protected_paths")  # fmt: skip
_save_lock = threading.Lock()
# owner/name at the end of any git remote URL: git@host:owner/name.git, https://host/owner/name, ssh://...
_REMOTE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")
_RULE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def overrides_version(overrides: Mapping[str, Any]) -> str:
    """A short fingerprint of the saved overrides; a device sends the one it saw so a stale save is refused."""
    return hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:16]


def _merged(overrides: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """The overrides after ``changes``; a change to None resets that key to its default."""
    merged = dict(overrides)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _checked(current: Settings, changes: Mapping[str, Any]) -> Settings:
    try:
        return settings_module.build(current.defaults, _merged(current.overrides, changes))
    except SettingsError as exc:
        raise Refused("settings", "; ".join(exc.problems)) from exc


def _changes(args: dict[str, Any], current: Settings) -> dict[str, Any]:
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise Refused("settings", "send the changed settings as {key: value}, with null to reset a key to its default")
    base = args.get("base")
    if base is not None and base != overrides_version(current.overrides):
        raise Refused("stale", "the settings changed since you opened them; review the new values, then save again")
    return changes


# ---- settings.toml in the warden's clone ---------------------------------------------------------------------


def _value_end(line: str, start: int) -> int:
    """Where a TOML value ends on its line: at the first # outside a string."""
    quote, i = "", start
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return i
        i += 1
    return len(line)


def set_line(text: str, key: str, value: Any) -> str:
    """``text`` with the one line holding ``key`` set to ``value``, keeping the line's comment where it was."""
    group, _, name = key.partition(".")
    section = ""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            section = stripped[1 : stripped.index("]")].strip()
            continue
        match = re.match(rf"^\s*{re.escape(name)}\s*=\s*", line)
        if section != group or match is None:
            continue
        end = _value_end(line, match.end())
        new = settings_module.toml_value(value)
        comment = line[end:]
        if comment:
            new = new.ljust(max(end - match.end(), len(new) + 1))
        lines[index] = line[: match.end()] + new + comment
        return "\n".join(lines)
    raise KeyError(f"settings.toml has no line for {key}")


def _leaf(tree: Mapping[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        node = node[part]
    return node


def _git(clone: Path, args: list[str], timeout: int) -> str:
    try:
        done = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, timeout=timeout,
                              check=False)  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise ops.StepError(f"git {args[0]} took longer than {timeout} s") from exc
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        raise ops.StepError(f"git {args[0]} failed: {last[:200]}")
    return done.stdout.strip()


def clone_of(settings: Settings) -> Path:
    """The warden's clone of the factory repo; an empty or relative setting would point git at the wrong repo."""
    clone = Path(settings.get_str("factory.clone"))
    if not clone.is_absolute() or not (clone / ".git").exists():
        raise ops.StepError("factory.clone is not the warden's clone of the factory repo; reinstall to set it")
    return clone


def commit_settings(clone: Path, values: Mapping[str, Any], changed: list[str], timeout: int) -> str:
    """Write ``values`` into the clone's settings.toml, commit and push; returns the commit the repo now holds."""
    _git(clone, ["checkout", "--", "settings.toml"], timeout)
    try:
        _git(clone, ["pull", "--rebase", "--quiet"], timeout)
    except ops.StepError:
        subprocess.run(["git", "-C", str(clone), "rebase", "--abort"], capture_output=True, check=False)
        raise
    path = clone / "settings.toml"
    text = path.read_text()
    new = text
    for key in sorted(values):
        new = set_line(new, key, values[key])
    tree = tomllib.loads(new)
    wrong = [key for key in values if _leaf(tree, key) != values[key]]
    if wrong:
        raise ops.StepError(f"settings.toml would not hold {', '.join(wrong)} as saved")
    if new != text:
        store.write_text(path, new)
        _git(clone, ["add", "settings.toml"], timeout)
        _git(clone, ["commit", "--quiet", "-m", "Settings: " + ", ".join(changed)], timeout)
    _git(clone, ["push", "--quiet"], timeout)
    return _git(clone, ["rev-parse", "HEAD"], timeout)


# ---- what reads the changed values ------------------------------------------------------------------------------


# The implementer's launcher takes the large rung's values; a session's start passes the rung's own model and effort.
_LAUNCHER_ROLES = {"implementer": "implementer_large"}
_PLACEHOLDERS = ("{{model}}", "{{effort}}")


def _role_of(agent: str) -> str | None:
    """The [models] role a launcher takes its model from: qa-backend is qa_backend, critic-ux critic."""
    role = "critic" if agent.startswith("critic-") else _LAUNCHER_ROLES.get(agent, agent.replace("-", "_"))
    return role if f"models.{role}" in settings_module.SCHEMA else None


def render_plugin(places: Places, settings: Settings) -> None:
    """Copy the installed plugin, with each launcher's model and effort filled in, into public/ where sessions read
    it, with the procedures and rules its skills reach beside it; each folder is swapped in whole."""
    target = places.rendered_plugin.parent
    for name in ("plugin", "procedures", "rules"):
        staging = target / f".{name}.new"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(places.install / name, staging, symlinks=False)
    for agent in sorted((target / ".plugin.new" / "agents").glob("*.md")):
        text = agent.read_text()
        if not any(mark in text for mark in _PLACEHOLDERS):
            continue
        role = _role_of(agent.stem)
        if role is None:
            raise ValueError(f"agents/{agent.name} asks for a model, but no [models] setting is its")
        model, effort = settings.model(role)
        agent.write_text(text.replace("{{model}}", model).replace("{{effort}}", effort))
    for name in ("plugin", "procedures", "rules"):
        old = target / f".{name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if (target / name).exists():
            (target / name).rename(old)
        (target / f".{name}.new").rename(target / name)
        shutil.rmtree(old, ignore_errors=True)


def apply_slice(settings: Settings) -> None:
    """Resize the slice every factory session runs in (polkit lets the warden set this one unit's properties)."""
    argv = [
        "systemctl",
        "set-property",
        SLICE,
        f"MemoryHigh={settings['budget.slice_memory_high']}",
        f"MemoryMax={settings['budget.slice_memory_max']}",
        f"CPUWeight={settings['budget.slice_cpu_weight']}",
    ]
    timeout = settings.get_int("factory.command_seconds")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ops.StepError(f"systemctl took longer than {timeout} s") from exc
    if done.returncode != 0:
        raise ops.StepError(f"systemctl set-property failed: {done.stderr.strip()[-200:]}")


# ---- the save ---------------------------------------------------------------------------------------------------


def _audit(ctx: Context, record: dict[str, Any]) -> None:
    store.append_jsonl(ctx.places.audit, {"at": ctx.clock.iso(), "by": ctx.caller.role, **record})


def _steps(ctx: Context, changes: dict[str, Any], result: dict[str, Any], commit: bool) -> list[ops.Step]:
    """The save's steps; with ``commit`` false (the repo's own values, which never enter the factory repo) the audit
    is written with the values instead of with the commit."""
    keys = sorted(changes)
    before = ctx.settings()
    saved: dict[str, Settings] = {}

    def save_values() -> str:
        with _save_lock:
            new = _checked(ctx.settings(), changes)
            store.write_text(ctx.places.overrides, settings_module.overrides_toml(new.overrides), mode=0o600)
            public.write_settings(ctx.places, new)
        saved["new"] = new
        if not commit:
            _audit(ctx, {"changes": _diff(before, new, keys), "commit": None})
        return f"{len(keys)} {'value' if len(keys) == 1 else 'values'} in effect"

    def launchers() -> str:
        render_plugin(ctx.places, saved["new"])
        return "the next subagents start with them"

    def resize() -> str:
        apply_slice(saved["new"])
        return "within a minute"

    def record() -> str:
        new = saved["new"]
        # The product's own values (its path, its hosts) never enter the factory repo.
        values = {key: new[key] for key in {*new.overrides, *keys} if key not in READ_ONLY and key not in REPO_KEYS}
        try:
            sha = commit_settings(clone_of(new), values, keys, new.get_int("factory.command_seconds"))
        except Exception as exc:
            _audit(ctx, {"changes": _diff(before, new, keys), "commit": None, "error": type(exc).__name__})
            raise
        result["commit"] = sha
        _audit(ctx, {"changes": _diff(before, new, keys), "commit": sha})
        return sha[:12]

    steps: list[ops.Step] = [("Save the new values", save_values)]
    if any(key.startswith("models.") for key in keys):
        steps.append(("Update the subagents' models", launchers))
    if any(key.startswith("budget.slice_") for key in keys):
        steps.append(("Resize the factory's slice", resize))
    if commit:
        steps.append(("Commit settings.toml to the factory repo", record))
    return steps


def _diff(before: Settings, after: Settings, keys: list[str]) -> list[dict[str, Any]]:
    return [{"key": key, "was": before[key], "now": after[key]} for key in keys]


def save(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Save the owner's changed settings (coded): refused at once if any value fails; otherwise an operation."""
    current = ctx.settings()
    changes = _changes(args, current)
    fixed = sorted(key for key in changes if key in READ_ONLY)
    if fixed:
        raise Refused("settings", f"{', '.join(fixed)} {'is' if len(fixed) == 1 else 'are'} set by the installer")
    repo = sorted(key for key in changes if key in REPO_KEYS)
    if repo:
        raise Refused("settings", "the repo's values change only with Change repo")
    _checked(current, changes)
    result: dict[str, Any] = {}
    return {"op": ops.launch(ctx, "settings", _steps(ctx, changes, result, commit=True), result)}


def _owner_name(remote: str) -> str:
    match = _REMOTE.search(remote.strip())
    return match.group(1).lower() if match else remote.strip().lower()


def change_repo(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Point the factory at another product repo (coded): never its own, only with every factory session retired."""
    current = ctx.settings()
    changes = _changes(args, current)
    unknown = sorted(key for key in changes if key not in REPO_KEYS)
    if unknown or "repo.path" not in changes:
        raise Refused("repo", f"send repo.path and any of {', '.join(REPO_KEYS[1:])}, nothing else")
    live = [s for s in registry.read(ctx.places).values() if s.state != RETIRED and s.role != OWNER]
    if live:
        raise Refused("repo", f"stop the factory first: {len(live)} sessions still work on the current repo")
    new = _checked(current, changes)
    path = Path(new.get_str("repo.path"))
    if not path.is_absolute():
        raise Refused("repo", "repo.path must be an absolute path to the factory's checkout of the product")
    try:
        clone = clone_of(new)
        own = _owner_name(_git(clone, ["remote", "get-url", "origin"], new.get_int("factory.command_seconds")))
    except ops.StepError as exc:
        raise Refused("repo", f"the factory cannot tell its own repo ({exc.detail})") from exc
    for place in (ctx.places.install, clone):
        if path == place or place in path.parents:
            raise Refused("repo", "the factory never works on itself")
    code, out = ctx.launcher.agent_command(["git", "remote", "get-url", "origin"], str(path))
    if code != 0:
        raise Refused("repo", f"{path} is not a git checkout with an origin remote")
    if own in (_owner_name(out), _owner_name(new.get_str("repo.github"))):
        raise Refused("repo", "the factory never works on its own repo")
    result: dict[str, Any] = {}

    def shape() -> str:
        argv = [str(FACTORY_BIN), "check", "--only", "repo-shape"]
        code, out = ctx.launcher.agent_command(argv, str(path))
        if code != 0:
            raise Refused("repo-shape", (out.strip().splitlines() or ["the repo-shape check failed"])[-1][:300])
        return "the standard shape"

    steps = [("Check the repo's shape", shape), *_steps(ctx, changes, result, commit=False)]
    return {"op": ops.launch(ctx, "repo", steps, result)}


# ---- guard rule switches ----------------------------------------------------------------------------------------


def known_rules() -> list[str]:
    """The guard's rule ids, from the guard itself, so a switch can only name a rule that exists."""
    return sorted(importlib.import_module("runtime.guard.rules").RULES)


def switch_rule(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Switch one guard rule on or off (coded); takes effect on the guard's next call."""
    rule, on = args.get("rule"), args.get("on")
    if not isinstance(rule, str) or not _RULE.match(rule) or rule not in known_rules():
        raise Refused("rule", "name one of the guard's rules, as the Guard page lists them")
    if not isinstance(on, bool):
        raise Refused("rule", "say on: true or on: false")
    with _save_lock:
        off = public.read_switches(ctx.places)
        if on:
            off.discard(rule)
        else:
            off.add(rule)
        public.write_switches(ctx.places, off)
    _audit(ctx, {"rule": rule, "on": on})
    return {"rule": rule, "on": on, "off": sorted(off)}
