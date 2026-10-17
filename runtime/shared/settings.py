"""The factory's one validated settings object.

Defaults live in the repo's settings.toml; the owner's saved overrides live in the warden's state. Both are read
once, merged and checked against SCHEMA; any problem stops the load with one error listing every problem. Nothing
else in the runtime reads a number, model or time from anywhere but here.
"""

from __future__ import annotations

import copy
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.shared.settings_schema import SCHEMA, Spec


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


_PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
# A check that could write Claude Code's settings, a tool's configuration, a program on a login shell's PATH or a key
# would act outside its walls the next time something read it, so no writable cache lies under these.
_UNWRITABLE = frozenset({".claude", ".config", ".local", ".ssh", "bin"})
_WEIGHT_KEYS = ("severity", "goal", "requests", "users", "age")
_KIB = {"K": 1, "M": 1024, "G": 1024**2, "T": 1024**3}


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
    return isinstance(value, str) and value[:-1].isdigit() and value[-1:] in _KIB


def places_for_test_runs(setting: Callable[[str], Any], ceiling: int) -> int:
    """How many checks run tests at once: the factory's memory before it slows, less the reserve, in test ceilings."""
    high: str = setting("budget.slice_memory_high")
    reserve: int = setting("budget.test_reserve_mib")
    return (int(high[:-1]) * _KIB[high[-1]] // 1024 - reserve) // ceiling


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
    check = _KINDS.get(spec.kind)
    if check is None:
        return [f"{key} has an unknown kind {spec.kind}"]
    return check(key, spec, value, tree)


def _models(tree: Mapping[str, Any], name: str) -> Any:
    """``models.<name>`` (the allowed models, or the efforts) as the tree being checked holds it."""
    return tree.get("models", {}).get(name, [])


def _number_problems(key: str, spec: Spec, value: Any, _: Mapping[str, Any]) -> list[str]:
    whole = spec.kind == "int"
    if isinstance(value, bool) or not isinstance(value, int if whole else int | float):
        return [f"{key} must be a whole number" if whole else f"{key} must be a number"]
    if spec.low is not None and value < spec.low or spec.high is not None and value > spec.high:
        return [f"{key} must be from {spec.low:g} to {spec.high:g}"]
    return []


def _text_problems(key: str, spec: Spec, value: Any, _: Mapping[str, Any]) -> list[str]:
    return [f"{key} must be set"] if not isinstance(value, str) or spec.required and not value.strip() else []


def _model_problems(key: str, _: Spec, value: Any, tree: Mapping[str, Any]) -> list[str]:
    if not isinstance(value, Mapping) or set(value) != {"model", "effort"}:
        return [f"{key} must name a model and an effort"]
    out = [] if value["model"] in _models(tree, "allowed") else [f"{key}: {value['model']} is not an allowed model"]
    return out + ([] if value["effort"] in _models(tree, "efforts") else [f"{key}: {value['effort']} is not an effort"])


def _weights_problems(key: str, _: Spec, value: Any, __: Mapping[str, Any]) -> list[str]:
    if not isinstance(value, Mapping) or set(value) != set(_WEIGHT_KEYS):
        return [f"{key} must weigh {', '.join(_WEIGHT_KEYS)}"]
    bad = [k for k, w in value.items() if isinstance(w, bool) or not isinstance(w, int | float) or w < 0]
    return [f"{key}.{k} must be a number from 0" for k in bad]


def _prices_problems(_: str, __: Spec, value: Any, tree: Mapping[str, Any]) -> list[str]:
    if not isinstance(value, Mapping):
        return ["prices must be a table of models"]
    allowed = _models(tree, "allowed")
    out = []
    for model, price in value.items():
        if model not in allowed:
            out.append(f"prices: {model} is not an allowed model")
        elif not isinstance(price, Mapping) or set(price) != set(_PRICE_KEYS):
            out.append(f"prices.{model} must set {', '.join(_PRICE_KEYS)}")
        elif any(isinstance(p, bool) or not isinstance(p, int | float) or p < 0 for p in price.values()):
            out.append(f"prices.{model}: each price must be a number from 0")
    return out


# Each kind of setting's check: (key, spec, value, the whole tree) to the problems with the value.
_KINDS: dict[str, Callable[[str, Spec, Any, Mapping[str, Any]], list[str]]] = {
    "int": _number_problems,
    "float": _number_problems,
    "str": _text_problems,
    "list": lambda key, spec, value, _: _list_problems(key, spec, value),
    "size": lambda key, _, value, __: [] if _size_ok(value) else [f"{key} must be a size such as 12G"],
    "model": _model_problems,
    "weights": _weights_problems,
    "prices": _prices_problems,
}


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
        # telemetry: surfaced — a missing setting is one of the `problems` the load raises
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
        # telemetry: surfaced — a missing setting is one of the `problems` the load raises
        except KeyError:
            if key != "prices":
                problems.append(f"{key} is missing")
            continue
        problems += problems_for(key, value, tree)
    problems = problems or _between(tree)
    if problems:
        raise SettingsError(problems)
    return Settings(tree, defaults, dict(overrides))


_BELOW = (
    ("vitals.memory_red_gib", "vitals.memory_amber_gib"),
    ("vitals.battery_red", "vitals.battery_amber"),
    ("cost.plan_amber_percent", "cost.plan_red_percent"),
)


def _between(tree: dict[str, Any]) -> list[str]:
    problems = [f"{low} must be below {high}" for low, high in _BELOW if _leaf(tree, low) >= _leaf(tree, high)]
    if places_for_test_runs(lambda key: _leaf(tree, key), _leaf(tree, "budget.test_memory_ceiling_mib")) < 1:
        problems.append(
            "budget.test_memory_ceiling_mib and budget.test_reserve_mib must fit under budget.slice_memory_high, "
            "or no check could run the tests"
        )
    return problems + [
        f"checks.writable_caches: {path!r} must be a folder inside the agent's home, such as .cache/uv, and not "
        f"under {', '.join(sorted(_UNWRITABLE))}"
        for path in tree["checks"]["writable_caches"] if not _home_folder(path)
    ]  # fmt: skip


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
