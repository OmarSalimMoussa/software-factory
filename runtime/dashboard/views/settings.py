"""Settings: every key of the settings schema as a row with its label and control, its saved value, its default and
what runs now; and the owner's edits turned back into the values the warden saves. The Claude account section at the
page's top is accounts.py's.

The table below covers every key in runtime/shared/settings.SCHEMA; a key missing from it fails the page loudly.
"""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views import accounts
from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, ago, epoch, model_label
from runtime.shared.models import RETIRED
from runtime.shared.settings import SCHEMA, Settings

WANTS = ("settings", "sessions", "slots", "accounts")
_EDITED_ELSEWHERE = {"models.allowed", "models.efforts", "prices", "factory.clone", "intake.weights"}
_ROLE_KEYS = {
    "implementer": None,
    "orchestrator": "orchestrator",
    "designer": "designer",
    "intake": "intake",
    "triage": "triage",
}
_TEXT_FLOATS = {"intake.minor_severity", "vitals.memory_amber_gib", "vitals.memory_red_gib"}

# (group, title, hint, [(key, label), ...]); hints say when a change takes effect.
GROUPS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    (
        "models",
        "Models",
        "A new model or effort applies when that kind of agent next starts. Agents already running "
        "keep theirs; none is stopped.",
        [
            ("models.orchestrator", "Orchestrator"),
            ("models.designer", "Designer"),
            ("models.implementer_large", "Implementer, large"),
            ("models.implementer_small", "Implementer, small"),
            ("models.intake", "Intake"),
            ("models.triage", "Triage"),
            ("models.critic", "Critics"),
            ("models.qa_backend", "qa-backend"),
            ("models.qa_security", "qa-security"),
            ("models.qa_ux", "qa-ux"),
            ("models.qa_visual", "qa-visual"),
            ("models.qa_ui_function", "qa-ui-function"),
            ("models.qa_regression", "qa-regression"),
            ("models.qa_prod", "qa-prod"),
            ("models.allowed", "Models agents may use"),
            ("models.efforts", "Efforts"),
        ],
    ),
    (
        "caps",
        "Caps",
        "Applies as soon as you save. Running agents keep running; a lower cap only holds back new starts.",
        [
            ("caps.implementers", "Implementers at once"),
            ("caps.designers", "Designers at once"),
            ("caps.subagents_at_once", "Subagents at once"),
        ],
    ),
    (
        "rounds",
        "Rounds",
        "Applies as soon as you save, to every task in progress.",
        [
            ("rounds.qa", "QA rounds"),
            ("rounds.critic", "Critic rounds"),
            ("rounds.check_runs", "Check runs on a merge turn"),
            ("rounds.extend_by", "One extension adds"),
        ],
    ),
    (
        "qa",
        "QA",
        "Applies to the next tester brief.",
        [
            ("qa.seconds_per_item", "Seconds per item"),
            ("qa.max_items", "Items per brief"),
            ("qa.desktop_seconds", "Desktop seconds per tester"),
            ("qa.evidence_days", "Days evidence is kept"),
            ("qa.widths", "Screen widths testers use"),
            ("qa.viewport_height", "Tester screen height"),
            ("qa.quiet_seconds", "Seconds of quiet before a tester's place frees"),
            ("qa.stale_seconds", "Seconds before a silent tester is let go"),
            ("qa.screenshots_for_owner_only", "Testers whose screenshots only you see"),
            ("qa.max_steps", "Steps a standing scenario may have"),
        ],
    ),
    (
        "intake",
        "Intake",
        "Applies to the next triage pass.",
        [
            ("intake.batch_size", "Triage batch size"),
            ("intake.batch_max_wait_hours", "Longest wait, hours"),
            ("intake.fast_lane_minutes", "Fast-lane minutes"),
            ("intake.weights", "Score weights"),
            ("intake.minor_severity", "A minor task's severity part"),
            ("intake.requests_full", "Requests that count in full"),
            ("intake.users_full", "People that count in full"),
            ("intake.age_full_days", "Days of age that count in full"),
        ],
    ),
    (
        "routing",
        "Routing",
        "Applies to the next proposal the orchestrator makes. Proposals already on Home stay.",
        [
            ("routing.max_proposals", "Proposals at once"),
            ("routing.small_max_slices", "Rung 1: most slices"),
            ("routing.large_max_slices", "Rung 2: most slices"),
            ("routing.estimate_min_runs", "Runs before an estimate shows"),
            ("routing.estimate_window", "Runs an estimate looks back on"),
            ("routing.cause_tries", "Tries to confirm a cause before asking for a higher rung"),
        ],
    ),
    (
        "cost",
        "Cost",
        "Applies as soon as you save. Nothing stops at the budget.",
        [
            ("cost.daily_budget_usd", "Daily budget at API prices, $"),
            ("cost.plan_stale_minutes", "Minutes before a plan reading counts as unknown"),
            ("cost.plan_amber_percent", "Plan bar turns amber at, %"),
            ("cost.plan_red_percent", "Plan bar turns red at, %"),
            ("prices", "Prices per million tokens"),
        ],
    ),
    (
        "watch",
        "Watch",
        "Applies as soon as you save.",
        [
            ("watch.loop_minutes", "Loop every, minutes"),
            ("watch.tick_seconds", "Warden checks every, seconds"),
            ("watch.probe_seconds", "Probe every, seconds"),
            ("watch.outage_after", "Failed prod probes in a row that mean an outage"),
            ("watch.http_timeout_seconds", "Seconds before a probe times out"),
            ("watch.inbox_wait_seconds", "Seconds the orchestrator's inbox wait lasts"),
            ("watch.push_wait_minutes", "Minutes a push to prod may take"),
            ("watch.deploy_minutes", "Minutes staging's deploy of a push may take"),
            ("watch.resume_tries", "Resumes to try before a crashed agent retires"),
            ("watch.bench_grace_seconds", "Seconds a pushed-out agent gets to commit"),
            ("watch.start_grace_seconds", "Seconds a starting agent gets to appear"),
            ("watch.reap_retry_minutes", "Try a failed clean-up again after, minutes"),
            ("watch.orchestrator_silent_minutes", "Restart a silent orchestrator after, minutes"),
            ("watch.orchestrator_busy_silent_minutes", "Restart a busy, silent orchestrator after, minutes"),
            ("watch.refusals_to_notify", "Refusals in a row that file an incident"),
            ("watch.refusal_window_minutes", "Minutes those refusals count over"),
            ("watch.socket_read_seconds", "Seconds a caller gets to send its request"),
            ("watch.message_seconds", "Seconds your message is held out to a session"),
        ],
    ),
    (
        "telemetry",
        "Errors and traces",
        "Applies as soon as you save.",
        [
            ("errors.poll_minutes", "Read prod's and staging's new errors every, minutes"),
            ("errors.max_pages", "Pages of errors one read covers"),
            ("errors.severe_events", "Events that make an error severe"),
            ("reports.poll_minutes", "Ask for users' new reports every, minutes"),
            ("telemetry.max_items", "Errors or traces one answer lists"),
            ("telemetry.max_kib", "Largest errors or traces answer, KiB"),
            ("telemetry.timeout_seconds", "Seconds a GlitchTip or Langfuse call may take"),
        ],
    ),
    (
        "dashboard",
        "Dashboard",
        "Applies as soon as you save; the port, addresses and threads at the next start.",
        [
            ("dashboard.retired_linger_minutes", "Minutes a retired agent stays in the live tree"),
            ("dashboard.refresh_seconds", "Refresh every, seconds"),
            ("dashboard.stale_seconds", "Seconds before data counts as old"),
            ("dashboard.page_size", "Rows per page"),
            ("dashboard.probe_strip", "Probes the outage band shows"),
            ("dashboard.working_now_max", "Agents under Working now"),
            ("dashboard.wrong_codes_to_lock", "Wrong codes before coded actions lock"),
            ("dashboard.lock_minutes", "Minutes coded actions stay locked"),
            ("dashboard.device_days", "Days a device stays signed in"),
            ("dashboard.idempotency_hours", "Hours a repeated action is recognised"),
            ("dashboard.port", "Dashboard port"),
            ("dashboard.hosts", "Addresses the dashboard answers"),
            ("dashboard.max_threads", "Requests served at once"),
            ("dashboard.warden_seconds", "Seconds the dashboard waits for the warden"),
            ("dashboard.settled_seconds", "Seconds a card you acted on says what happened"),
            ("dashboard.message_max_chars", "Longest message to a session, characters"),
            ("dashboard.thread_messages", "Your messages a session's thread shows"),
            ("dashboard.thread_tail_kib", "End of a transcript the thread reads, KiB"),
        ],
    ),
    (
        "slots",
        "Slots",
        "The slot count applies at the next slot claim; the rest as soon as you save.",
        [
            ("slots.count", "Agent slots"),
            ("slots.port_base", "First slot's port"),
            ("slots.log_tail_lines", "Lines of output an agent sees"),
            ("slots.api_timeout_seconds", "Seconds a slot call may take"),
            ("slots.api_max_kib", "Largest slot answer, KiB"),
            ("slots.verb_minutes", "Minutes one stack command may take"),
        ],
    ),
    (
        "checks",
        "Checks",
        "Applies to the next check.",
        [
            ("checks.run_minutes", "Minutes one check may take"),
            ("checks.test_seconds", "Seconds one test may take"),
            ("checks.output_kib", "Most of a check's report an agent sees, KiB"),
        ],
    ),
    (
        "budget",
        "Memory and CPU",
        "The factory's memory cap and CPU weight apply within a minute; a check's memory at the next install.",
        [
            ("budget.slice_memory_high", "Memory where the factory slows"),
            ("budget.slice_memory_max", "Factory memory cap"),
            ("budget.slice_cpu_weight", "Factory CPU weight"),
            ("budget.test_memory_ceiling_mib", "Memory one check may use, MiB"),
            ("budget.test_reserve_mib", "Memory kept free for tests, MiB"),
        ],
    ),
    (
        "vitals",
        "Laptop",
        "Applies as soon as you save.",
        [
            ("vitals.memory_amber_gib", "Free memory that turns amber, GiB"),
            ("vitals.memory_red_gib", "Free memory that turns red, GiB"),
            ("vitals.cpu_pressure_amber", "CPU pressure that turns amber, %"),
            ("vitals.battery_red", "Battery that turns red, %"),
            ("vitals.firewall_stale_seconds", "Firewall last loaded longer ago than this turns red, seconds"),
        ],
    ),
    (
        "subagents",
        "Subagents",
        "Applies to the next subagent each role starts.",
        [
            ("subagents.orchestrator", "The orchestrator may start"),
            ("subagents.designer", "A designer may start"),
            ("subagents.implementer", "An implementer may start"),
            ("subagents.browser_personas", "Testers with a browser"),
            ("subagents.unpermitted", "Allowed in a session the warden never started"),
        ],
    ),
    (
        "sandbox",
        "Sandbox",
        "Sites apply to each agent that starts after you save; cache folders at the next install, which makes them.",
        [
            ("sandbox.allowed_domains", "Sites agents may reach"),
            ("sandbox.writable_caches", "Tool caches agents may write, in their home"),
        ],
    ),
    (
        "records",
        "Queue and reflections",
        "Applies as soon as you save.",
        [
            ("queue.max_upload_kib", "Largest upload, KiB"),
            ("reflect.fold_hours", "Hours a repeated reflection folds into one"),
            ("reflect.max_chars", "Longest reflection, characters"),
        ],
    ),
    (
        "gc",
        "Nightly prune",
        "Applies to the next prune.",
        [
            ("gc.prune_hour", "Prune at, hour"),
            ("gc.images_keep_days", "Days unused images are kept"),
            ("gc.docker_budget_gib", "Docker disk budget, GiB"),
        ],
    ),
    (
        "repo",
        "Repo",
        "Change these with Change repo, with the factory stopped.",
        [
            ("repo.path", "Checkout"),
            ("repo.default_branch", "Default branch"),
            ("repo.github", "On GitHub"),
            ("repo.protected_paths", "Paths only you change"),
        ],
    ),
    (
        "factory",
        "Factory",
        "Set by the installer, except how long the warden waits for its own commands.",
        [
            ("factory.clone", "The warden's clone of the factory repo"),
            ("factory.command_seconds", "Seconds the warden waits for its own commands"),
        ],
    ),
    (
        "install",
        "Install",
        "Read by the installer: a change applies at the next install.",
        [
            ("install.drill", "Model the drills use"),
            ("install.drill_seconds", "Longest drill, seconds"),
            ("install.drills_at_once", "Drill sessions at once"),
            ("install.tailnet_seconds", "Seconds to publish the dashboard"),
            ("install.hook_timeout_seconds", "Seconds Claude Code waits for the guard"),
            ("install.firewall_refresh_seconds", "Load the firewall again every, seconds"),
            ("install.healthchecks_grace_minutes", "Minutes a check-in may be late"),
            ("install.toolchain", "Programs the product's scripts need"),
        ],
    ),
]
LABELS = {key: label for _, _, _, rows in GROUPS for key, label in rows}


def control(key: str) -> str:
    kind = SCHEMA[key].kind
    if key in _EDITED_ELSEWHERE or key.startswith("repo.") or kind in ("prices", "weights"):
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
    kind = SCHEMA[key].kind
    if kind == "model":
        return {"model": model_label(value["model"]), "effort": value["effort"]}
    if key == "models.allowed":
        return [model_label(m) for m in value]
    if kind == "list":
        return [str(v) for v in value]
    if kind == "prices":
        if not value:
            return "No prices set: every call's cost is unknown"
        return " · ".join(f"{model_label(m)} ${p['input']:g} in, ${p['output']:g} out" for m, p in value.items())
    if kind == "weights":
        return " · ".join(f"{name} {weight:g}" for name, weight in value.items())
    return value


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


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"accounts": lambda: accounts.section(snap), "settings": lambda: page(snap)}
