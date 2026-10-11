"""The owner's coded changes to the factory's tuning: settings, the product repo, and the guard's rule switches.

A save is checked whole before anything is written; a value that fails is refused with every reason. Then, as one
operation the dashboard follows: the overrides are written to the warden's state and take effect at once, and
whatever reads the changed values is refreshed (the published settings, the subagents' launchers for a model or
effort, the slice for a budget). The owner's values stay on this machine: the defaults are the installed release's,
and the warden never writes the factory repo. Every save and switch is audited with its values.
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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.shared.paths import SLICE, Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import AT_INSTALL, Settings, SettingsError
from runtime.warden import ops, permits, public, registry
from runtime.warden.context import Context
from runtime.warden.verbs import OWNER

log = logging.getLogger("factory.warden")

# Set by the installer; the dashboard shows them read-only.
READ_ONLY = frozenset({"models.allowed", "prices", "factory.github"})
REPO_KEYS = ("repo.path", "repo.default_branch", "repo.github", "repo.protected_paths")
_save_lock = threading.Lock()
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


# ---- what reads the changed values ------------------------------------------------------------------------------


# The implementer's launcher takes the large rung's values; a session's start passes the rung's own model and effort.
_LAUNCHER_ROLES = {"implementer": "implementer_large"}
_PLACEHOLDERS = ("{{model}}", "{{effort}}")


def _role_of(agent: str) -> str | None:
    """The [models] role a launcher takes its model from (settings.model_role), the implementer's the large rung's."""
    return _LAUNCHER_ROLES.get(agent) or settings_module.model_role(agent)


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


def _steps(ctx: Context, changes: dict[str, Any]) -> list[ops.Step]:
    keys = sorted(changes)
    before = ctx.settings()
    saved: dict[str, Settings] = {}

    def save_values() -> str:
        with _save_lock:
            new = _checked(ctx.settings(), changes)
            store.write_text(ctx.places.overrides, settings_module.overrides_toml(new.overrides), mode=0o600)
            public.write_settings(ctx.places, new)
        saved["new"] = new
        _audit(ctx, {"changes": _diff(before, new, keys)})
        return f"{len(keys)} {'value' if len(keys) == 1 else 'values'} in effect"

    def launchers() -> str:
        render_plugin(ctx.places, saved["new"])
        return "the next subagents start with them"

    def resize() -> str:
        apply_slice(saved["new"])
        return "within a minute"

    steps: list[ops.Step] = [("Save the new values", save_values)]
    if any(key.startswith("models.") for key in keys):
        steps.append(("Update the subagents' models", launchers))
    if any(key.startswith("budget.slice_") for key in keys):
        steps.append(("Resize the factory's slice", resize))
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
    wired = sorted(
        key for key in changes if key in settings_module.SCHEMA and settings_module.SCHEMA[key].when == AT_INSTALL
    )
    if wired:
        raise Refused("settings", f"{', '.join(wired)} {'is' if len(wired) == 1 else 'are'} wired in at install: "
                      f"install again with --set {wired[0]}=<value>")  # fmt: skip
    repo = sorted(key for key in changes if key in REPO_KEYS)
    if repo:
        raise Refused("settings", "the repo's values change only with Change repo")
    _checked(current, changes)
    return {"op": ops.launch(ctx, "settings", _steps(ctx, changes), {})}


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
    if not new.get_str("factory.github"):
        raise Refused("repo", "the factory cannot tell its own repo: reinstall, so factory.github names it")
    if path == ctx.places.install or ctx.places.install in path.parents:
        raise Refused("repo", "the factory never works on itself")
    code, out = ctx.launcher.agent_command(["git", "remote", "get-url", "origin"], str(path))
    if code != 0:
        raise Refused("repo", f"{path} is not a git checkout with an origin remote")
    if any(permits.is_factory_repo(new, remote) for remote in (out, new.get_str("repo.github"))):
        raise Refused("repo", "the factory never works on its own repo")

    def shape() -> str:
        code, out = ctx.launcher.run_check(str(path), ["--only", "repo-shape"])
        if code != 0:
            raise Refused("repo-shape", (out.strip().splitlines() or ["the repo-shape check failed"])[-1][:300])
        return "the standard shape"

    steps = [("Check the repo's shape", shape), *_steps(ctx, changes)]
    return {"op": ops.launch(ctx, "repo", steps, {})}


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
