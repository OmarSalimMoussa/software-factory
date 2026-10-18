"""Home: is prod safe, what should start next, what else needs the owner, and is the work moving."""

from __future__ import annotations

import html
from typing import Any

from runtime.dashboard.views import messaging
from runtime.dashboard.views.kit import Parts, Query, Snap, clock, epoch, iso, model_word, plural, span, when
from runtime.dashboard.views.shell import STATE_COLOR, github_url, health, prod_runs_staging
from runtime.dashboard.views.tasks import proposals
from runtime.dashboard.views.words import agentwords, taskwords
from runtime.shared.models import ON_STAGING, QA_CLEAN, RETIRED, WAITING

WANTS = (
    "settings",
    "probes",
    "outage",
    "proposals",
    "cards",
    "queue",
    "sessions",
    "cost",
    "vitals",
    "goal",
    "factory",
    "deploys",
    "rules",
    "triage",
    "upgrade",
    "yolo",
)
SPARK_DAYS = 14
_LAPTOP = {
    "green": ("healthy", "healthy", "good"),
    "amber": ("amber", "strained", "warn"),
    "unknown": ("amber", "unknown", "warn"),
    "red": ("red", "critical", "bad"),
}
_STAGES = (
    ("designing", "designing", "--f-design", ("designing",)),
    ("building", "building", "--f-impl", ("building", "deprioritized", "on-hold", "turn", "merged", "checked")),
    ("qa", "in QA", "--f-qa", ("qa", QA_CLEAN)),
    ("needs", "need you", "--warn", ("review", "decide", "blocked")),
)


def env_card(snap: Snap, env: str) -> dict[str, Any]:
    now = snap.now
    pill = health(snap, env)
    streak = snap.get("probes")["streak"].get(env)
    chips = []
    if pill["state"] in ("healthy", "down") and streak is not None:
        word, tone = ("healthy", "good") if pill["state"] == "healthy" else ("down", "bad")
        chips.append({"text": f"{word} {span(now - epoch(streak['since']))}", "tone": tone})
    if env == "staging":
        ahead = sum(1 for t in snap.get("queue")["tasks"] if t["status"] == ON_STAGING)
        if ahead:
            chips.append({"text": f"{ahead} {'task' if ahead == 1 else 'tasks'} ahead of prod", "tone": "accent"})
    elif "prod-frozen" not in snap.get("rules")["off"]:
        chips.append({"text": "frozen", "tone": "muted"})
    current = snap.get("deploys")[env]["current"]
    return {
        "name": env.title(),
        "state": pill["state"],
        "tone": pill["tone"],
        "state_word": pill["state"].title(),
        "state_color": STATE_COLOR[pill["state"]],
        "sha": pill["sha"],
        "deployed": when(epoch(current["at"])) if current else None,
        "chips": chips,
        "commit_url": f"{github_url(snap)}/commit/{pill['sha']}" if pill["sha"] else None,
    }


def laptop(snap: Snap) -> dict[str, Any]:
    vitals = snap.get("vitals")["reading"]
    if vitals is None:
        rows = [{"label": label, "pct": None, "value": "unknown"} for label in ("Memory", "CPU", "Power", "Disk")]
        return {"state": "amber", "tone": "warn", "label": "unknown", "rows": rows}
    readings = {r["name"]: r for r in vitals["readings"]}
    state, label, tone = _LAPTOP[vitals["level"]]
    cpu, power = readings["cpu"], readings["power"]
    cpu_pct, power_pct = cpu["figure"], power["figure"]
    mains = on_mains(power)
    used, disk = memory_used(snap), disk_free(readings.get("disk"))
    disk_used = None if disk is None else 100 - disk[1]
    cpu_words = f"{cpu_pct:.0f}%" if cpu_pct is not None else "unknown"
    power_words = "Mains" if mains else f"{power_pct:.0f}%" if power_pct is not None else power["value"]
    return {
        "state": state,
        "tone": tone,
        "label": label,
        "rows": [
            {"label": "Memory", "pct": used, "value": "unknown" if used is None else f"{used}%"},
            {"label": "CPU", "pct": round(cpu_pct) if cpu_pct is not None else None, "value": cpu_words},
            {
                "label": "Power",
                "pct": 100 if mains else (round(power_pct) if power_pct is not None else None),
                "value": "unknown" if power["level"] == "unknown" else power_words,
            },
            {"label": "Disk", "pct": disk_used, "value": "unknown" if disk_used is None else f"{disk_used}%"},
        ],
    }


def on_mains(power: dict[str, Any]) -> bool:
    """On battery, the power reading's figure is the charge, at any level; on mains it has none."""
    return power["level"] == "green" and power["figure"] is None


def memory_free(snap: Snap) -> tuple[float, int] | None:
    """Free memory in GiB and as a share of all memory, from the warden's last reading."""
    vitals = snap.get("vitals")
    reading = next((r for r in (vitals["reading"] or {}).get("readings", []) if r["name"] == "memory"), None)
    free = reading["figure"] if reading else None
    if free is None or not vitals["memory"]:
        return None
    return free, round(free * 1024 * 1024 / vitals["memory"] * 100)


def disk_free(reading: dict[str, Any] | None) -> tuple[float, int] | None:
    """Free disk in GiB and as a share of the disk, from the warden's last reading: None until a reading has both."""
    if reading is None or reading["figure"] is None or not reading["whole"]:
        return None
    return reading["figure"], round(reading["figure"] / reading["whole"] * 100)


def memory_used(snap: Snap) -> int | None:
    free = memory_free(snap)
    return None if free is None else 100 - free[1]


def envs(snap: Snap) -> dict[str, Any]:
    ahead = sum(1 for t in snap.get("queue")["tasks"] if t["status"] == ON_STAGING)
    return {
        "staging": env_card(snap, "staging"),
        "prod": env_card(snap, "prod"),
        "to_push": ahead,
        # Between the cards when no task waits on staging: staging can still differ from prod (a push back, a revert).
        "step_word": "in step" if prod_runs_staging(snap) else "differs",
        "laptop": laptop(snap),
    }


def outage(snap: Snap) -> dict[str, Any] | None:
    found = snap.get("outage")
    if found is None:
        return None
    settings = snap.settings
    strip = settings.get_int("dashboard.probe_strip")
    recent = snap.get("probes")["recent"]["prod"]
    marks: list[bool | None] = [p["ok"] if p["ok"] or p["counts"] else None for p in recent]
    every = settings.get_int("watch.probe_seconds")
    pace = "one a minute" if every == 60 else f"one every {every} s"
    task = taskwords.tasks(snap).get(found["task"]) if found["task"] else None
    title = html.escape(task["title"]) if task else "the fix"
    fixer = snap.get("sessions").get(found["session"]) if found["session"] else None
    if found["stage"] == "fixing":
        how = f" · {model_word(fixer['model'])} · rung {fixer['rung']}" if fixer else ""
        message = f"The factory started a fix without you: <b>{title}</b>{how}. Everything else still waits for you."
        actions = ["watch", "stop"]
    elif found["stage"] == ON_STAGING:
        message, actions = f"The fix <b>{title}</b> is on staging. Push it to prod, or push prod back.", ["push"]
    elif found["stage"] == "stopped":
        message = "You stopped the fix. Prod stays down until you push a build or push prod back."
        actions = ["push"]
    else:
        message = "The factory may start one fix without you, at rung 1 or 2. Everything else still waits for you."
        actions = []
    live = fixer is not None and fixer["state"] != RETIRED
    return {
        "since": f"{clock(epoch(found['since']))} UTC",
        "probes": [None] * (strip - len(marks)) + marks,
        "probe_note": f"last {strip} probes, {pace}",
        "stage": found["stage"],
        "message": message,
        "session": found["session"] if live else None,
        "actions": actions,
    }


def tiles(snap: Snap) -> dict[str, Any]:
    settings = snap.settings
    queue = snap.get("queue")
    counts = {key: sum(1 for t in queue["tasks"] if t["status"] in states) for key, _, _, states in _STAGES}
    triage = snap.get("triage")
    batch = settings.get_int("intake.batch_size")
    days = snap.get("cost")["days"]
    names = [iso(snap.now - n * 86400)[:10] for n in range(SPARK_DAYS - 1, -1, -1)]
    spark = [round(sum(c[0] for c in days.get(d, {}).get("family", {}).values()), 2) for d in names]
    unknown = int(sum(c[1] for c in days.get(names[-1], {}).get("family", {}).values()))
    budget = settings.get_float("cost.daily_budget_usd")
    # Calls of unknown price add an unknown amount: today's known spend is the least it cost, and with no price
    # known at all the day's spend is unknown, never $0.
    today = None if unknown and not spark[-1] else spark[-1]
    pct = None if today is None else round(today / budget * 100)
    foot = (f"{pct}% of the daily budget" if pct is not None
            else "unknown: no price is set for its models, so the budget stops nothing")  # fmt: skip
    if unknown and today is not None:
        foot += f", and {plural(unknown, 'call')} of unknown price"
    return {
        "work": {
            "count": sum(counts.values()),
            "stages": [
                {"key": key, "label": label, "count": counts[key], "color": color} for key, label, color, _ in _STAGES
            ],
        },
        "staging": {
            "count": sum(1 for t in queue["tasks"] if t["status"] == ON_STAGING),
            "foot": "waiting to push to prod",
        },
        "requests": {
            "count": triage["waiting"],
            "batch": batch,
            "triage_at": triage["due_at"],
            "foot_right": f"or at {batch}",
        },
        "spend": {
            "today": today,
            "budget": budget,
            "pct": pct,
            "spark": spark,
            "foot": foot,
        },
    }


def goal(snap: Snap) -> dict[str, Any]:
    """Each goal item's bar is its tasks on prod over its tasks; Done once the owner checks it done in GOAL.md, the
    one place that says so."""
    found = snap.get("goal")
    queue = snap.get("queue")
    items = []
    for item in found["items"]:
        mine = [t for t in queue["tasks"] if t["goal_item"] == item["id"] and _counts(t)]
        live = sum(1 for t in mine if _on_prod(t))
        progress = live / len(mine) if mine else 0.0
        if item["done"]:
            state = "Done"
        elif any(t["status"] not in ("queued", "parked", "proposed") for t in mine):
            state = "Building"
        else:
            state = "Not started"
        items.append({"id": item["id"], "name": item["text"], "progress": round(progress, 2), "state": state})
    return {"doc": "GOAL.md", "items": items}


def _on_prod(task: dict[str, Any]) -> bool:
    return task["status"] == "promoted" or task["status"] == "closed" and str(task["last_move"]["from"]) == "promoted"


def _counts(task: dict[str, Any]) -> bool:
    """A dropped task, or noise, is not part of its goal item."""
    return task["status"] != "closed" or _on_prod(task)


def working(snap: Snap) -> list[dict[str, Any]]:
    rows = [
        s
        for s in snap.get("sessions").values()
        if s["state"] in ("working", "waiting", "reviving") and s["role"] != "orchestrator"
    ]
    rows = sorted(rows, key=lambda s: s["started"])[: snap.settings.get_int("dashboard.working_now_max")]
    return [
        {
            "session": s["id"],
            "family": s["family"],
            "initials": agentwords.initials(s),
            "name": agentwords.name(snap, s),
            "doing": agentwords.doing(snap, s),
            "dot": "warn" if s["state"] == WAITING else "accent",
        }
        for s in rows
    ]


_COMES_BACK = "The dashboard restarts during the upgrade. This page comes back on its own."


def upgrade(snap: Snap) -> dict[str, Any] | None:
    """The upgrade under way, how it ended, or nothing; the words are all here."""
    found = snap.get("upgrade")
    band = _upgrade_asked(found) if found["asked"] is not None else None
    return None if band is None else {**band, "busy": band["state"] in ("preparing", "running")}


def _upgrade_asked(found: dict[str, Any]) -> dict[str, Any] | None:
    """The upgrade the owner asked for, from its getting ready to its end; None once the factory runs again."""
    asked, progress, op = found["asked"], found["progress"], found["op"]
    resume = ["resume"] if found["paused"] else []
    blank: dict[str, Any] = {"log": None, "actions": []}
    to = asked["to"][:7]
    mine = progress if progress and progress["to"] == asked["to"] and progress["at"] >= asked["at"] else None
    if mine is None and op is not None and op["state"] in ("waiting", "running", "failed"):
        step = next((s for s in op["steps"] if s["state"] in ("running", "failed")), {"text": "", "detail": ""})
        if op["state"] == "failed":
            return {**blank, "state": "failed", "title": "The upgrade did not start", "message": step["detail"],
                    "actions": resume}  # fmt: skip
        return {**blank, "state": "preparing", "title": f"Getting ready to upgrade to {to}",
                "message": f"{step['text']}. Every agent keeps its work."}  # fmt: skip
    if mine is None or mine["state"] == "running":
        return {**blank, "state": "running", "title": f"Upgrading the factory to {to}",
                "message": f"{mine['step'] if mine else 'Starting'}. {_COMES_BACK}"}  # fmt: skip
    if mine["state"] == "failed":
        return {**blank, "state": "failed", "title": "The upgrade stopped", "message": mine["problem"],
                "log": mine["log"] or None, "actions": resume}  # fmt: skip
    if resume:
        return {**blank, "state": "done", "title": f"The factory is upgraded to {to}", "message": "Your agents are "
                "paused with their work kept; restart the factory when you are ready. The warden pushes the product's "
                f"move to this factory itself. Then run the server set-up from a factory checkout at {to} "
                "(docs/server.md).", "actions": resume}  # fmt: skip
    return None


def yolo(snap: Snap) -> dict[str, Any] | None:
    """The band while YOLO mode is on: the factory runs on its own, and pushes nothing."""
    found = snap.get("yolo")
    if found is None:
        return None
    since = when(epoch(found["since"]))
    return {
        "title": "YOLO mode is on",
        "message": f"Since {since}, the factory approves, decides and answers on its own. Nothing is pushed.",
        "button": "Turn off",
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS), "days": SPARK_DAYS}


def parts(snap: Snap, query: Query) -> Parts:
    return {
        "envs": lambda: envs(snap),
        "outage": lambda: outage(snap),
        "upgrade": lambda: upgrade(snap),
        "yolo": lambda: yolo(snap),
        "proposals": lambda: proposals.slots(snap),
        "needs": lambda: proposals.needs(snap),
        "tiles": lambda: tiles(snap),
        "goal": lambda: goal(snap),
        "working": lambda: working(snap),
        "message": lambda: messaging.orchestrator(snap),
    }
