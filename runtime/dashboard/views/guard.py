"""Guard: the laptop's vitals and Docker disk, the slots, what was refused today, and every rule with its switch."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.home import memory_free, on_mains
from runtime.dashboard.views.kit import Parts, Query, Snap
from runtime.dashboard.views.words import agentwords
from runtime.shared.models import RETIRED

WANTS = ("settings", "vitals", "slots", "sessions", "queue", "refusals", "rules", "docker")
_TONES = {"green": "good", "amber": "warn", "red": "bad", "unknown": "", "missing": ""}
_GIB = 1024**3


def vitals(snap: Snap) -> list[dict[str, Any]]:
    reading = snap.get("vitals")["reading"]
    found = {r["name"]: r for r in (reading or {}).get("readings", [])}
    out = []
    memory = memory_free(snap)
    level = found.get("memory", {}).get("level", "unknown")
    out.append(
        {
            "label": "Memory free",
            "value": "unknown" if memory is None else f"{memory[0]:.1f} GiB",
            "pct": None if memory is None else memory[1],
            "tone": _TONES[level],
        }
    )
    cpu = found.get("cpu")
    pct = cpu["figure"] if cpu else None
    missing = cpu is not None and cpu["level"] == "missing"
    out.append(
        {
            "label": "CPU pressure",
            "value": f"{pct:.0f}%" if pct is not None else "not reported" if missing else "unknown",
            "pct": round(pct) if pct is not None else None,
            "tone": _TONES[cpu["level"] if cpu else "unknown"],
        }
    )
    power = found.get("power")
    if power is None or power["level"] == "unknown":
        out.append({"label": "Power", "value": "unknown", "pct": None, "tone": ""})
    else:
        battery = power["figure"]
        mains = on_mains(power)
        out.append(
            {
                "label": "Power",
                "value": "On mains" if mains else f"Battery {battery:.0f}%" if battery is not None else "On battery",
                "pct": 100 if mains else round(battery) if battery is not None else None,
                "tone": _TONES[power["level"]],
            }
        )
    install = found.get("install", {"level": "unknown"})
    words = {"green": "Matches", "red": "Differs: reinstall", "unknown": "unknown"}
    out.append(
        {
            "label": "Install",
            "value": words.get(install["level"], "unknown"),
            "pct": None if install["level"] == "unknown" else 100,
            "tone": _TONES[install["level"]],
        }
    )
    return out


def slots(snap: Snap) -> list[dict[str, Any]]:
    found = snap.get("slots")
    sessions = snap.get("sessions").values()
    orchestrator = any(s["role"] == "orchestrator" and s["state"] != RETIRED for s in sessions)
    out = [
        {
            "n": 0,
            "who": "Orchestrator",
            "note": "check of the default branch",
            "port": found["main_port"],
            "up": orchestrator,
        }
    ]
    by_name = {s["name"]: s for s in sessions if s["state"] != RETIRED}
    by_name.update({s["id"]: s for s in sessions if s["state"] != RETIRED})
    for slot in found["rows"]:
        lease = slot["lease"]
        if lease is None:
            out.append({"n": slot["n"], "who": None, "note": "free", "port": slot["port"], "up": False})
            continue
        holder = by_name.get(lease["holder"])
        who = agentwords.name(snap, holder) if holder else lease["holder"]
        out.append({"n": slot["n"], "who": who, "note": lease["note"] or "held", "port": slot["port"], "up": True})
    return out


def refusals(snap: Snap) -> list[dict[str, Any]]:
    counts = snap.get("refusals")
    return [{"rule": rule, "count": n} for rule, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def rules(snap: Snap) -> list[dict[str, Any]]:
    found, counts = snap.get("rules"), snap.get("refusals")
    off = set(found["off"])
    return [
        {"id": rule, "on": rule not in off, "today": f"{counts.get(rule, 0)} refused today", "hint": what}
        for rule, what in found["rules"].items()
    ]


def docker(snap: Snap) -> dict[str, Any] | None:
    found = snap.get("docker")
    if found is None:
        return None
    budget = found["budget_bytes"]
    if found["bytes"] is None:
        return {"used": "unknown", "budget": f"{budget / _GIB:.0f} GiB", "pct": None, "tone": ""}
    pct = round(found["bytes"] / budget * 100)
    tone = "bad" if found["bytes"] > budget else "good"
    return {"used": f"{found['bytes'] / _GIB:.0f} GiB", "budget": f"{budget / _GIB:.0f} GiB", "pct": pct, "tone": tone}


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {
        "vitals": lambda: vitals(snap),
        "slots": lambda: slots(snap),
        "refusals": lambda: refusals(snap),
        "rules": lambda: rules(snap),
        "docker": lambda: docker(snap),
    }
