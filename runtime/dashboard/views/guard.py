"""Guard: the machine's vitals and Docker disk, the slots, what was refused today, and every rule with its switch."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.home import disk_free, memory_free, on_mains
from runtime.dashboard.views.kit import Parts, Query, Snap, ago, epoch, span
from runtime.dashboard.views.words import agentwords
from runtime.shared.models import RETIRED

WANTS = ("settings", "vitals", "slots", "sessions", "queue", "refusals", "rules", "gc")
_TONES = {"green": "good", "amber": "warn", "red": "bad", "unknown": ""}
_GIB = 1024**3
_KINDS = (("worktrees", "Agents' worktrees"), ("docker", "Docker"), ("qa", "QA recordings"),
          ("transcripts", "transcripts"))  # fmt: skip


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
            "hint": None,
        }
    )
    disk = disk_free(found.get("disk"))
    out.append(
        {
            "label": "Disk free",
            "value": "unknown" if disk is None else f"{disk[0]:.0f} GiB",
            "pct": None if disk is None else disk[1],
            "tone": _TONES[found.get("disk", {}).get("level", "unknown")],
            "hint": disk_detail(snap),
        }
    )
    cpu = found.get("cpu")
    pct = cpu["figure"] if cpu else None
    out.append(
        {
            "label": "CPU use",
            "value": f"{pct:.0f}%" if pct is not None else "unknown",
            "pct": round(pct) if pct is not None else None,
            "tone": _TONES[cpu["level"] if cpu else "unknown"],
            "hint": None,
        }
    )
    power = found.get("power")
    if power is None or power["level"] == "unknown":
        out.append({"label": "Power", "value": "unknown", "pct": None, "tone": "", "hint": None})
    else:
        battery = power["figure"]
        mains = on_mains(power)
        out.append(
            {
                "label": "Power",
                "value": "On mains" if mains else f"Battery {battery:.0f}%" if battery is not None else "On battery",
                "pct": 100 if mains else round(battery) if battery is not None else None,
                "tone": _TONES[power["level"]],
                "hint": None,
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
            "hint": None,
        }
    )
    return out


def disk_detail(snap: Snap) -> str:
    found, every = snap.get("gc"), snap.settings.get_int("gc.every_hours")
    if found is None:
        return f"What fills the disk is not measured yet: the clean-up measures it every {every} h."
    said = [found["problem"][:1].upper() + found["problem"][1:] + "."] if found["problem"] else []
    if found["measured"] is None:
        said.append(f"What fills the disk is not measured yet: the clean-up measures it every {every} h.")
    else:
        use = found["use"]
        sizes = [f"{word} {'unknown' if use[kind] is None else f'{use[kind] / _GIB:.1f} GiB'}" for kind, word in _KINDS]
        said.append(f"{', '.join(sizes)}, measured {ago(epoch(found['measured']), snap.now)}.")
    due = epoch(found["started"]) + every * 3600 - snap.now
    said.append("A clean-up is running." if found["ended"] is None else f"The next clean-up is in {span(due)}.")
    return " ".join(said)


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
    found = snap.get("gc")
    if found is None or found["measured"] is None:
        return None
    used, budget = found["use"]["docker"], snap.settings.get_int("gc.docker_budget_gib") * _GIB
    if used is None:
        return {"used": "unknown", "budget": f"{budget / _GIB:.0f} GiB", "pct": None, "tone": ""}
    pct = round(used / budget * 100)
    tone = "bad" if used > budget else "good"
    return {"used": f"{used / _GIB:.0f} GiB", "budget": f"{budget / _GIB:.0f} GiB", "pct": pct, "tone": tone}


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
