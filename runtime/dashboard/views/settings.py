"""Settings: every key of the settings schema as a row with its label and control, its saved value, its default and
what runs now; and the owner's edits turned back into the values the warden saves. The Claude account section at the
page's top is accounts.py's.

The labels in words/settingwords.py cover every key in runtime/shared/settings_schema.SCHEMA; a key missing from them
fails the page loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.dashboard.views import accounts
from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, ago, epoch, model_label
from runtime.dashboard.views.words.settingwords import GROUPS, LABELS
from runtime.shared.models import RETIRED
from runtime.shared.settings import Settings
from runtime.shared.settings_schema import AT_INSTALL, SCHEMA

WANTS = ("settings", "sessions", "slots", "accounts", "upgrade", "yolo")
_EDITED_ELSEWHERE = {"models.allowed", "models.efforts", "prices", "factory.github", "intake.weights"}
_ROLE_KEYS = {
    "implementer": None,
    "orchestrator": "orchestrator",
    "designer": "designer",
    "intake": "intake",
    "triage": "triage",
}
_TEXT_FLOATS = {"intake.minor_severity", "vitals.memory_amber_gib", "vitals.memory_red_gib"}


def control(key: str) -> str:
    kind = SCHEMA[key].kind
    if (
        key in _EDITED_ELSEWHERE
        or key.startswith("repo.")
        or kind in ("prices", "weights")
        or SCHEMA[key].when == AT_INSTALL
    ):
        return "readonly"
    if kind == "model":
        return "model"
    if kind == "list":
        return "list"
    if kind == "int" or kind == "float" and key not in _TEXT_FLOATS:
        return "number"
    return "text"


def shown(key: str, value: Any) -> Any:
    """A value as its row shows it: model names by their labels, tables as one line."""
    if key == "models.allowed":
        return [model_label(m) for m in value]
    show = _SHOWN.get(SCHEMA[key].kind)
    return value if show is None else show(value)


def _prices(value: dict[str, Any]) -> str:
    if not value:
        return "No prices set: every call's cost is unknown, so the daily budget stops nothing"
    return " · ".join(f"{model_label(m)} ${p['input']:g} in, ${p['output']:g} out" for m, p in value.items())


_SHOWN: dict[str, Callable[[Any], Any]] = {
    "model": lambda value: {"model": model_label(value["model"]), "effort": value["effort"]},
    "list": lambda value: [str(v) for v in value],
    "prices": _prices,
    "weights": lambda value: " · ".join(f"{name} {weight:g}" for name, weight in value.items()),
}


def _default(settings: Settings, key: str) -> Any:
    """The value in settings.toml; a key the file leaves out (only prices may) defaults to none."""
    node: Any = settings.defaults
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node


def saved(settings: Settings, key: str) -> Any:
    """The value in effect; prices may be left out of the settings entirely, and then none are set."""
    return settings.tree().get("prices", {}) if key == "prices" else settings[key]


def _running(snap: Snap, key: str) -> Any:
    """What the factory runs with now, where that can lag the saved value: live sessions' models, the slots."""
    settings = snap.settings
    if key == "slots.count":
        held = [s["n"] for s in snap.get("slots")["rows"] if s["lease"] is not None]
        return max([settings.get_int(key), *held])
    role = key.removeprefix("models.")
    if key.startswith("models.") and role in _ROLE_KEYS.values():
        for s in snap.get("sessions").values():
            if s["state"] != RETIRED and s["role"] == role:
                return {"model": s["model"], "effort": s["effort"]}
    return saved(settings, key)


def row(snap: Snap, key: str) -> dict[str, Any]:
    settings = snap.settings
    spec = SCHEMA[key]
    value = saved(settings, key)
    applied = _running(snap, key)
    pending = None
    if applied != value:
        pending = (
            f"{shown(key, value)} at the next slot claim"
            if key == "slots.count"
            else (f"{shown(key, value)['model']} · {value['effort']} at the next start")
        )
    number = control(key) == "number"
    allowed = settings.get_list("models.allowed")
    return {
        "key": key,
        "label": LABELS[key],
        "control": control(key),
        "value": shown(key, value),
        "saved": shown(key, value),
        "default": shown(key, _default(settings, key)),
        "applied": shown(key, applied),
        "pending": pending,
        "low": spec.low if number else None,
        "high": spec.high if number else None,
        "step": 1 if number else None,
        "hint": f"From {spec.low:g} to {spec.high:g}." if spec.low is not None and spec.high is not None else None,
        "options": {"models": [model_label(m) for m in allowed], "efforts": settings.get_list("models.efforts")}
        if spec.kind == "model"
        else None,
        "when": spec.when,
    }


def page(snap: Snap) -> dict[str, Any]:
    raw = snap.get("settings")
    missing = sorted(set(SCHEMA) - set(LABELS))
    if missing:
        raise PartError(f"The settings page has no row for {', '.join(missing)}.")
    audit = []
    for entry in reversed(raw["audit"]):
        if "rule" in entry:
            what = f"Rule {entry['rule']} switched {'on' if entry['on'] else 'off'}"
        else:
            what = "; ".join(_change_words(c) for c in entry["changes"])
        sha = (entry.get("commit") or "")[:7] or None
        audit.append({"what": what, "sha": sha, "ago": ago(epoch(entry["at"]), snap.now)})
    return {
        "repo": {"path": snap.settings.get_str("repo.path")},
        "groups": [
            {"key": g, "title": title, "hint": hint, "rows": [row(snap, key) for key, _ in rows]}
            for g, title, hint, rows in GROUPS
        ],
        "audit": audit,
    }


def _change_words(change: dict[str, Any]) -> str:
    key = change["key"]
    label = LABELS.get(key, key)
    was, now = shown(key, change["was"]), shown(key, change["now"])
    if SCHEMA[key].kind == "model":
        return f"{label} to {now['model']} · {now['effort']}"
    return f"{label} {_plain(was)} → {_plain(now)}"


def _plain(value: Any) -> str:
    return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)


def to_saved(settings: Settings, key: str, value: Any) -> Any:
    """An edit as the warden saves it: model labels back to model ids, numbers typed as text read as numbers.
    None resets the key to its default."""
    if key not in SCHEMA or control(key) == "readonly":
        raise PartError(f"{LABELS.get(key, key)} cannot be changed here.")
    if value is None:
        return None
    kind = SCHEMA[key].kind
    if kind == "model":
        ids = {model_label(m): m for m in settings.get_list("models.allowed")}
        if not isinstance(value, dict) or value.get("model") not in ids:
            raise PartError(f"{LABELS[key]}: choose one of the allowed models.")
        return {"model": ids[value["model"]], "effort": value.get("effort")}
    if kind in ("int", "float") and isinstance(value, str):
        try:
            return int(value) if kind == "int" else float(value)
        except ValueError as exc:
            raise PartError(f"{LABELS[key]} must be a number.") from exc
    if kind == "list" and isinstance(value, list) and all(isinstance(v, int) for v in _default(settings, key)):
        if not all(isinstance(v, int) or isinstance(v, str) and v.isdigit() for v in value):
            raise PartError(f"{LABELS[key]} holds whole numbers only.")
        return [int(v) for v in value]
    return value


def factory(snap: Snap) -> dict[str, Any]:
    """The commit of the factory installed here."""
    installed = snap.get("upgrade")["installed"]
    return {"version": installed[:7] if installed else "Version unknown"}


def mode(snap: Snap) -> dict[str, Any]:
    """YOLO mode, on or off, the button that switches it, and what turning it on means, said once before it does."""
    on = snap.get("yolo") is not None
    return {
        "on": on,
        "title": "YOLO mode is on" if on else "YOLO mode is off",
        "text": "The factory approves, decides and answers on its own. Nothing is pushed."
        if on
        else "You approve, decide and answer.",
        "button": "Turn off" if on else "Turn on",
        "confirm": {
            "title": "Turn on YOLO mode",
            "text": "The factory runs without you until you switch this off. It approves its own proposals as places "
            "free, its orchestrator says Go on specs and decides stuck tasks, and any question an agent asks you is "
            "answered: be proactive and solve it yourself. Nothing is pushed: the work waits on Home for your Push to "
            "staging. The daily budget still applies.",
            "button": "Turn on",
        },
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {
        "accounts": lambda: accounts.section(snap),
        "factory": lambda: factory(snap),
        "mode": lambda: mode(snap),
        "settings": lambda: page(snap),
    }
