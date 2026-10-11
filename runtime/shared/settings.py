"""The factory's one validated settings object.

Defaults live in the repo's settings.toml; the owner's saved overrides live in the warden's state. Both are read
once, merged and checked against SCHEMA; any problem stops the load with one error listing every problem. Nothing
else in the runtime reads a number, model or time from anywhere but here.
"""

from __future__ import annotations

import copy
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


def model_role(kind: str) -> str | None:
    """The [models] role an agent or subagent kind runs on: qa-backend is qa_backend, and every critic shares
    critic. None for a kind with no model of its own to set, such as Explore."""
    role = "critic" if kind.startswith("critic-") else kind.replace("-", "_")
    return role if f"models.{role}" in SCHEMA else None


class SettingsError(Exception):
    """The settings cannot be used; the message lists every problem."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("settings: " + "; ".join(problems))


@dataclass(frozen=True, slots=True)
class Spec:
    """What one setting may hold, and when a change to it takes effect."""

    kind: str  # int, float, str, bool, size, list, model, prices, weights
    low: float | None = None  # for a list, the fewest items it may hold
    high: float | None = None  # for a list, the most
    required: bool = False
    when: str = "At once"
    item: str = "str"  # for a list, each item's kind: str (never blank) or int (above zero)


AT_ONCE, NEXT_START, MINUTE, ORCH = "At once", "Next start", "Within a minute", "Next orchestrator start"
# Wired by the install into what nothing else rewrites (the firewall, the tailnet's serve entry, the check-in periods):
# changed only with the install's --set, so a save never leaves them out of step.
AT_INSTALL = "Set at install"
_ROLES = (
    "orchestrator designer implementer_large implementer_small intake triage critic qa_backend qa_security qa_ux "
    "qa_visual qa_ui_function qa_regression qa_prod"
).split()

SCHEMA: dict[str, Spec] = {
    "repo.path": Spec("str", required=True, when=ORCH),
    "repo.default_branch": Spec("str", required=True, when=ORCH),
    "repo.github": Spec("str", required=True, when=ORCH),
    # contract: the product's extra paths only a person changes, beyond the factory's own protected set.
    "repo.protected_paths": Spec("list", required=True),
    "models.allowed": Spec("list", 1, required=True, when=NEXT_START),
    "models.efforts": Spec("list", 1, required=True, when=NEXT_START),
    **{f"models.{role}": Spec("model", when=NEXT_START) for role in _ROLES},
    "caps.implementers": Spec("int", 1, 6),
    "caps.designers": Spec("int", 1, 4),
    "caps.subagents_at_once": Spec("int", 1, 8),
    "caps.browsers_at_once": Spec("int", 1, 16),
    "subagents.browser_personas": Spec("list"),
    "subagents.orchestrator": Spec("list"),
    "subagents.designer": Spec("list"),
    "subagents.implementer": Spec("list"),
    "subagents.unpermitted": Spec("list"),
    "rounds.qa": Spec("int", 1, 8),
    "rounds.critic": Spec("int", 1, 8),
    "rounds.check_runs": Spec("int", 1, 6),
    "rounds.extend_by": Spec("int", 1, 4),
    "qa.seconds_per_item": Spec("int", 30, 600),
    "qa.max_items": Spec("int", 1, 20),
    "qa.prod_every_hours": Spec("int", 1, 168),
    "qa.desktop_seconds": Spec("int", 60, 900),
    "qa.evidence_days": Spec("int", 1, 60),
    "qa.widths": Spec("list", 1, 6, item="int"),
    "qa.quiet_seconds": Spec("int", 30, 900),
    "qa.stale_seconds": Spec("int", 60, 3600),
    "qa.screenshots_for_owner_only": Spec("list"),
    "qa.max_steps": Spec("int", 1, 20),  # contract: the most numbered steps a standing scenario has
    "qa.viewport_height": Spec("int", 400, 2000),  # contract: the tester browser's height; widths come from qa.widths
    "intake.batch_size": Spec("int", 10, 500),
    "intake.batch_max_wait_hours": Spec("int", 1, 48),
    "intake.fast_lane_minutes": Spec("int", 5, 60),
    "intake.weights": Spec("weights"),
    "intake.minor_severity": Spec("float", 0, 1),
    "intake.requests_full": Spec("int", 2, 200),
    "intake.users_full": Spec("int", 2, 200),
    "intake.age_full_days": Spec("int", 1, 90),
    "intake.rescore_hours": Spec("int", 1, 48),
    "routing.small_max_slices": Spec("int", 1, 3),
    "routing.large_max_slices": Spec("int", 1, 6),
    "routing.max_proposals": Spec("int", 1, 5),
    "routing.estimate_min_runs": Spec("int", 1, 50),
    "routing.estimate_window": Spec("int", 1, 200),
    "routing.cause_tries": Spec("int", 1, 10),
    "slots.count": Spec("int", 1, 6, when="At the next slot claim"),
    "slots.port_base": Spec("int", 1024, 65000, when=NEXT_START),
    "slots.log_tail_lines": Spec("int", 0, 500),
    "slots.api_timeout_seconds": Spec("int", 1, 300),  # contract: one slot API or health call
    "slots.api_max_kib": Spec("int", 16, 8192),  # contract: the largest slot API answer a session sees
    "slots.verb_minutes": Spec("int", 1, 240),  # contract: added by builder 3, one scripts/dev verb
    "budget.slice_memory_high": Spec("size", when=MINUTE),
    "budget.slice_memory_max": Spec("size", when=MINUTE),
    "budget.slice_cpu_weight": Spec("int", 1, 10000, when=MINUTE),
    "budget.test_memory_ceiling_mib": Spec("int", 512, 65536, when="Next install"),
    "budget.test_reserve_mib": Spec("int", 0, 65536),
    "vitals.memory_amber_gib": Spec("float", 0.5, 256),
    "vitals.memory_red_gib": Spec("float", 0.25, 256),
    "vitals.cpu_pressure_amber": Spec("float", 1, 100),
    "vitals.battery_red": Spec("int", 0, 100),
    "vitals.firewall_stale_seconds": Spec("int", 30, 3600),
    "watch.tick_seconds": Spec("int", 5, 120),
    "watch.probe_seconds": Spec("int", 15, 600, when=AT_INSTALL),
    "watch.outage_after": Spec("int", 2, 5),
    "watch.http_timeout_seconds": Spec("int", 1, 60),  # contract: added by 1B-2
    "watch.inbox_wait_seconds": Spec("int", 5, 300),  # contract: added by 1B-2
    "watch.push_wait_minutes": Spec("int", 5, 240),  # contract: added by 1B-2
    "watch.deploy_minutes": Spec("int", 5, 240),
    "watch.ci_poll_minutes": Spec("int", 1, 60),
    "watch.branch_retry_minutes": Spec("int", 1, 60),
    "watch.resume_tries": Spec("int", 1, 5),
    "watch.crash_window_minutes": Spec("int", 5, 1440),
    "watch.resume_backoff_seconds": Spec("int", 5, 600),
    "watch.daemon_settle_seconds": Spec("int", 0, 300),
    "watch.bench_grace_seconds": Spec("int", 10, 300),
    "watch.reap_retry_minutes": Spec("int", 1, 1440),
    "watch.start_grace_seconds": Spec("int", 30, 600),  # contract: a start's process must appear within this
    "watch.message_seconds": Spec("int", 3, 60),  # contract: how long the owner's message is held out to a session
    "watch.loop_minutes": Spec("int", 5, 120, when=AT_INSTALL),
    "watch.orchestrator_silent_minutes": Spec("int", 10, 180),
    "watch.orchestrator_busy_silent_minutes": Spec("int", 10, 600),
    "watch.refusals_to_notify": Spec("int", 1, 50),
    "watch.refusal_window_minutes": Spec("int", 5, 1440),
    "watch.socket_read_seconds": Spec("int", 1, 120),  # contract: added by 1B-3, a request line must arrive by then
    "queue.max_upload_kib": Spec("int", 16, 8192),
    "checks.run_minutes": Spec("int", 1, 240),
    "checks.test_seconds": Spec("int", 1, 600),
    "checks.output_kib": Spec("int", 4, 1024),
    "errors.poll_minutes": Spec("int", 1, 120),  # contract: added by FB6, prod's GlitchTip issues become requests
    "errors.max_pages": Spec("int", 1, 50),
    "errors.severe_events": Spec("int", 1, 1_000_000),
    "reports.poll_minutes": Spec("int", 1, 1440),
    "telemetry.max_items": Spec("int", 1, 200),  # contract: added by FB6, `factory errors` and `factory traces`
    "telemetry.max_kib": Spec("int", 4, 1024),
    "telemetry.timeout_seconds": Spec("int", 1, 120),
    "reflect.fold_hours": Spec("int", 1, 168),
    "reflect.max_chars": Spec("int", 80, 4000),  # contract: added by 1B-3
    "cost.daily_budget_usd": Spec("float", 1, 100000),
    "cost.plan_stale_minutes": Spec("int", 5, 240),
    "cost.plan_amber_percent": Spec("int", 1, 100),
    "cost.plan_red_percent": Spec("int", 1, 100),
    "prices": Spec("prices"),
    "sandbox.allowed_domains": Spec("list", when=NEXT_START),
    # contract: added by FB8. Folders in the agent's home every session may write; the install makes them.
    "sandbox.writable_caches": Spec("list", when="Next install"),
    "dashboard.port": Spec("int", 1024, 65535, when=AT_INSTALL),
    "dashboard.refresh_seconds": Spec("int", 5, 120),
    "dashboard.stale_seconds": Spec("int", 15, 600),
    "dashboard.page_margin_seconds": Spec("int", 1, 60),
    "dashboard.wrong_codes_to_lock": Spec("int", 1, 20),
    "dashboard.lock_minutes": Spec("int", 5, 1440),
    "dashboard.device_days": Spec("int", 1, 365),
    "dashboard.hosts": Spec("list", 1, when=AT_INSTALL),
    "dashboard.page_size": Spec("int", 10, 500),
    "dashboard.probe_strip": Spec("int", 3, 60),
    "dashboard.working_now_max": Spec("int", 1, 24),
    "dashboard.max_threads": Spec("int", 2, 64, when="Next warden start"),
    "dashboard.retired_linger_minutes": Spec("int", 1, 30),
    "dashboard.idempotency_hours": Spec("int", 1, 168),  # contract: added by 1B-3
    "dashboard.warden_seconds": Spec("int", 1, 60),  # contract: added by 2A, its longest wait on the warden
    "dashboard.settled_seconds": Spec("int", 1, 30),  # contract: added by 2A, an acted-on card says so this long
    "dashboard.message_max_chars": Spec("int", 100, 20000),  # contract: the longest message the owner sends
    "dashboard.thread_messages": Spec("int", 1, 50),  # contract: the owner's messages a session's thread shows
    "dashboard.thread_tail_kib": Spec("int", 64, 65536),  # contract: how much of a transcript the thread reads
    "gc.prune_hour": Spec("int", 0, 23),
    "gc.images_keep_days": Spec("int", 1, 90),
    "gc.docker_budget_gib": Spec("int", 5, 2000),
    # The factory's own repo on GitHub (owner/name), which it never works on; the installer sets it.
    "factory.github": Spec("str", when="Next install"),
    "factory.command_seconds": Spec("int", 10, 900),
    "factory.warden_threads": Spec("int", 2, 64, when="Next warden start"),  # contract: added by 1B-3
    # contract: added by builder 3, read by the installer only.
    "install.drill": Spec("model", when="Next install"),
    "install.drill_seconds": Spec("int", 30, 900, when="Next install"),
    "install.drills_at_once": Spec("int", 1, 8, when="Next install"),
    "install.tailnet_seconds": Spec("int", 10, 600, when="Next install"),
    "install.hook_timeout_seconds": Spec("int", 2, 60, when="Next install"),
    "install.firewall_refresh_seconds": Spec("int", 10, 600, when="Next install"),
    "install.healthchecks_grace_minutes": Spec("int", 1, 120, when="Next install"),
    # contract: added by FB7. The programs the product's scripts need, copied from the owner's PATH for the agents and
    # the stack.
    "install.toolchain": Spec("list", when="Next install"),
    "install.claude_lowest": Spec("str", when="Next install"),
    "install.claude_untested": Spec("str", when="Next install"),
}

_PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
# A session that could write Claude Code's settings, a tool's configuration, a program on a login shell's PATH or a
# key would act outside its sandbox the next time something read it, so no writable cache lies under these.
_UNWRITABLE = frozenset({".claude", ".config", ".local", ".ssh", "bin"})
_WEIGHT_KEYS = ("severity", "goal", "requests", "users", "age")


def _leaf(tree: Mapping[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def _set_leaf(tree: dict[str, Any], key: str, value: Any) -> None:
    *heads, last = key.split(".")
    node = tree
    for part in heads:
        node = node.setdefault(part, {})
    node[last] = value


def _home_folder(path: object) -> bool:
    """A folder inside a home, named relative to it, outside the folders no session may write."""
    if not isinstance(path, str):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and not path.startswith("/") and ".." not in parts and parts[0] not in _UNWRITABLE


def _size_ok(value: object) -> bool:
    return isinstance(value, str) and value[:-1].isdigit() and value[-1:] in ("K", "M", "G", "T")


def _list_problems(key: str, spec: Spec, value: Any) -> list[str]:
    """A list, item by item: one that saves must work where it is read (no tester browser opens at no width)."""
    if not isinstance(value, list):
        return [f"{key} must be a list"]
    if spec.low is not None and len(value) < spec.low:
        return [f"{key} must hold at least {spec.low:g}"]
    if spec.high is not None and len(value) > spec.high:
        return [f"{key} may hold at most {spec.high:g}"]
    if spec.item == "int":
        good = all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value)
        return [] if good else [f"{key} must hold whole numbers above zero"]
    good = all(isinstance(v, str) and v.strip() for v in value)
    return [] if good else [f"{key} must hold text, none of it blank"]


def problems_for(key: str, value: Any, tree: Mapping[str, Any]) -> list[str]:
    """Why ``value`` cannot be the setting ``key``; empty when it can."""
    spec = SCHEMA.get(key)
    if spec is None:
        return [f"{key} is not a setting"]
    allowed = tree.get("models", {}).get("allowed", [])
    efforts = tree.get("models", {}).get("efforts", [])
    if spec.kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        return [f"{key} must be a whole number"]
    if spec.kind == "float" and (isinstance(value, bool) or not isinstance(value, int | float)):
        return [f"{key} must be a number"]
    if spec.kind in ("int", "float"):
        if spec.low is not None and value < spec.low or spec.high is not None and value > spec.high:
            return [f"{key} must be from {spec.low:g} to {spec.high:g}"]
        return []
    if spec.kind == "str":
        if not isinstance(value, str) or spec.required and not value.strip():
            return [f"{key} must be set"]
        return []
    if spec.kind == "list":
        return _list_problems(key, spec, value)
    if spec.kind == "size":
        return [] if _size_ok(value) else [f"{key} must be a size such as 12G"]
    if spec.kind == "model":
        if not isinstance(value, Mapping) or set(value) != {"model", "effort"}:
            return [f"{key} must name a model and an effort"]
        out = [] if value["model"] in allowed else [f"{key}: {value['model']} is not an allowed model"]
        return out + ([] if value["effort"] in efforts else [f"{key}: {value['effort']} is not an effort"])
    if spec.kind == "weights":
        if not isinstance(value, Mapping) or set(value) != set(_WEIGHT_KEYS):
            return [f"{key} must weigh {', '.join(_WEIGHT_KEYS)}"]
        bad = [k for k, w in value.items() if isinstance(w, bool) or not isinstance(w, int | float) or w < 0]
        return [f"{key}.{k} must be a number from 0" for k in bad]
    if spec.kind == "prices":
        if not isinstance(value, Mapping):
            return ["prices must be a table of models"]
        out = []
        for model, price in value.items():
            if model not in allowed:
                out.append(f"prices: {model} is not an allowed model")
            elif not isinstance(price, Mapping) or set(price) != set(_PRICE_KEYS):
                out.append(f"prices.{model} must set {', '.join(_PRICE_KEYS)}")
            elif any(isinstance(p, bool) or not isinstance(p, int | float) or p < 0 for p in price.values()):
                out.append(f"prices.{model}: each price must be a number from 0")
        return out
    return [f"{key} has an unknown kind {spec.kind}"]


def read_some(tree: Any, keys: Sequence[str]) -> dict[str, Any]:
    """``keys`` alone, each checked as a setting, from a settings tree another release may have written: an older
    install's published settings lack the keys this release added, and hold ones it dropped. Raises SettingsError
    listing every problem with the keys asked for."""
    root: Mapping[str, Any] = tree if isinstance(tree, Mapping) else {}
    found: dict[str, Any] = {}
    problems: list[str] = []
    for key in keys:
        try:
            value = _leaf(root, key)
        except KeyError:
            problems.append(f"{key} is missing")
            continue
        problems += problems_for(key, value, root)
        found[key] = value
    if problems:
        raise SettingsError(problems)
    return found


class Settings:
    """The effective settings: defaults with the owner's overrides applied, already checked."""

    def __init__(self, tree: dict[str, Any], defaults: dict[str, Any], overrides: dict[str, Any]) -> None:
        self._tree = tree
        self.defaults = defaults
        self.overrides = overrides

    def __getitem__(self, key: str) -> Any:
        return _leaf(self._tree, key)

    def get_int(self, key: str) -> int:
        value = self[key]
        assert isinstance(value, int) and not isinstance(value, bool), key
        return value

    def get_float(self, key: str) -> float:
        value = self[key]
        assert isinstance(value, int | float) and not isinstance(value, bool), key
        return float(value)

    def get_str(self, key: str) -> str:
        value = self[key]
        assert isinstance(value, str), key
        return value

    def get_list(self, key: str) -> list[Any]:
        value = self[key]
        assert isinstance(value, list), key
        return value

    def model(self, role: str) -> tuple[str, str]:
        entry = self[f"models.{role}"]
        return entry["model"], entry["effort"]

    def price(self, model: str) -> dict[str, float] | None:
        """A model's prices per million tokens, or None when unset (its cost is then unknown)."""
        entry = self._tree.get("prices", {}).get(model)
        return {k: float(v) for k, v in entry.items()} if entry else None

    def tree(self) -> dict[str, Any]:
        return self._tree

    def with_changes(self, changes: Mapping[str, Any]) -> Settings:
        """These settings with ``changes`` applied, checked; raises SettingsError listing every problem."""
        merged = dict(self.overrides)
        merged.update(changes)
        return build(self.defaults, merged)


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if path in SCHEMA:
            flat[path] = value
        elif isinstance(value, Mapping):
            flat.update(flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


def build(defaults: dict[str, Any], overrides: Mapping[str, Any]) -> Settings:
    """Merge ``overrides`` (flat keys) onto ``defaults`` and check every value."""
    tree = copy.deepcopy(defaults)
    problems = [f"{key} is not a setting" for key in overrides if key not in SCHEMA]
    for key, value in overrides.items():
        if key in SCHEMA:
            _set_leaf(tree, key, value)
    flat = flatten(tree)
    problems += [f"{key} is not a setting" for key in flat if key not in SCHEMA]
    for key in SCHEMA:
        try:
            value = _leaf(tree, key)
        except KeyError:
            if key != "prices":
                problems.append(f"{key} is missing")
            continue
        problems += problems_for(key, value, tree)
    if not problems and tree["vitals"]["memory_red_gib"] >= tree["vitals"]["memory_amber_gib"]:
        problems.append("vitals.memory_red_gib must be below vitals.memory_amber_gib")
    if not problems and tree["cost"]["plan_amber_percent"] >= tree["cost"]["plan_red_percent"]:
        problems.append("cost.plan_amber_percent must be below cost.plan_red_percent")
    if not problems:
        problems += [
            f"sandbox.writable_caches: {path!r} must be a folder inside the agent's home, such as .cache/uv, and not "
            f"under {', '.join(sorted(_UNWRITABLE))}"
            for path in tree["sandbox"]["writable_caches"] if not _home_folder(path)
        ]  # fmt: skip
    if problems:
        raise SettingsError(problems)
    return Settings(tree, defaults, dict(overrides))


def load(defaults_path: Path, overrides_path: Path | None) -> Settings:
    """The effective settings from the defaults file and, when it exists, the owner's overrides file."""
    try:
        defaults = tomllib.loads(defaults_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError([f"cannot read {defaults_path}: {exc}"]) from exc
    overrides: dict[str, Any] = {}
    if overrides_path is not None and overrides_path.exists():
        try:
            overrides = flatten(tomllib.loads(overrides_path.read_text()))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SettingsError([f"cannot read {overrides_path}: {exc}"]) from exc
    return build(defaults, overrides)


def overrides_toml(overrides: Mapping[str, Any]) -> str:
    """The overrides as TOML text, one table per group, in a stable order."""
    groups: dict[str, dict[str, Any]] = {}
    for key in sorted(overrides):
        group, _, name = key.partition(".")
        if name:
            groups.setdefault(group, {})[name] = overrides[key]
        else:  # a whole table, such as prices
            groups.setdefault(group, {}).update(overrides[key])
    lines: list[str] = []
    for group, entries in groups.items():
        lines.append(f"[{group}]")
        lines += [f"{name} = {toml_value(value)}" for name, value in entries.items()]
        lines.append("")
    return "\n".join(lines)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")
