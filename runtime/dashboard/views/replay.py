"""One tester run's replay page: its items as rows with their verdicts, each step's expected and got, and every beat
the player shows, with its words, its icon and its place on the screen worked out here. The page only lays it out
and keeps the time."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, day_label, epoch, when
from runtime.dashboard.views.words import taskwords
from runtime.shared.models import SEVERITIES

WANTS = ("settings", "queue", "replay")
FIELDS = ("of", "head", "tester", "run")
PACE = (
    "freeze",
    "card",
    "fade",
    "between",
    "char",
    "read",
    "glide",
    "before",
    "after",
    "miss",
    "type_char",
    "type_lead",
    "type_rest",
    "pill",
    "shot",
)
# (icon, tone, the word a step's result reads as)
RESULTS = {
    "pass": ("check-circle", "good", "Passed"),
    "fail": ("x-circle", "bad", "Failed"),
    "blocked": ("no-symbol", "warn", "Blocked"),
    "none": ("ellipsis-horizontal", "muted", "No result was recorded"),
    "skip": ("minus-circle", "muted", "Didn't run"),
}
BEAT_ICONS = {
    "think": "chat-bubble-left-right",
    "go": "globe-alt",
    "click": "cursor-arrow-rays",
    "type": "bars-3-bottom-left",
    "key": "bars-3-bottom-left",
    "read": "eye",
    "wait": "clock",
    "resize": "device-phone-mobile",
    "shot": "camera",
    "miss": "exclamation-triangle",
}
SEVERITY_TONES = dict(zip(SEVERITIES, ("bad", "warn", "muted"), strict=True))  # worst first
HIDDEN_SAYS = "A sign-in link was on screen here, so this part is hidden."
_FRAME = re.compile(r"^replay/frames/\d{6}\.jpg$")
_SHOT = re.compile(r"^[A-Za-z0-9][\w.-]{0,127}\.(?:png|jpe?g)$")


def url(of: str, head: str, tester: str, run: str) -> str:
    """The replay page's address in the API, for a link to it."""
    return "/api/replay?" + urlencode({"of": of, "head": head[:12], "tester": tester, "run": run})


def evidence_base(of: str, head: str, tester: str, run: str) -> str:
    return "/api/evidence/" + "/".join((of, head[:12], tester, run)) + "/"


def _asked(query: Query) -> dict[str, str]:
    missing = [k for k in FIELDS if not query.get(k)]
    if missing:
        raise PartError(
            f"A replay is named by its task or check, head, tester and run; this one has no {', '.join(missing)}."
        )
    return {k: str(query[k]) for k in FIELDS}


def wants(query: Query) -> dict[str, Any]:
    asked = _asked(query)
    return {"want": list(WANTS), "replay": asked}


def parts(snap: Snap, query: Query) -> Parts:
    return {"replay": lambda: page(snap, _asked(query))}


def page(snap: Snap, asked: dict[str, str]) -> dict[str, Any]:
    found = snap.get("replay")
    entry, data = found["entry"], found["replay"]
    head = {"title": _title(snap, asked["of"]), "sub": f"{asked['tester']} · {when(epoch(str(entry['at'])))}"}
    if data is None:
        return head | {"state": "making", "says": "The replay is being made. It shows here once it is ready."}
    if "error" in data:
        return head | {"state": "failed", "says": f"This run has no replay: {data['error']}."}
    base = evidence_base(asked["of"], asked["head"], asked["tester"], asked["run"])
    shown = [f for f in data["frames"] if _FRAME.match(f["file"])]
    frames = [{"t": f["t"], "url": base + f["file"], "w": f["w"], "h": f["h"]} for f in shown]
    if not frames:
        return head | {"state": "failed", "says": "This run has no replay: it kept no pictures of the screen."}
    settings = snap.settings
    rows = [_row(item, frames, base) for item in data["items"]]
    return head | {
        "state": "ready",
        "summary": _summary(data["items"]),
        "frames": frames,
        "hidden": data["hidden"],
        "hidden_says": HIDDEN_SAYS,
        "seconds": data["seconds"],
        "pace": {name: settings.get_int(f"replay.{name}_ms") for name in PACE},
        "rows": rows,
        "empty": None if rows else "The tester marked no steps in this run, so it plays as one recording.",
    }


def _title(snap: Snap, of: str) -> str:
    task = taskwords.tasks(snap).get(of)
    check = next((c for c in snap.get("queue")["checks"] if c["id"] == of), None)
    return str(task["title"]) if task else str(check["title"]) if check else of


def _summary(items: list[dict[str, Any]]) -> dict[str, str]:
    total = len(items)
    for verdict, tone, word in (("fail", "bad", "failed"), ("blocked", "warn", "blocked")):
        n = sum(1 for item in items if item["verdict"] == verdict)
        if n:
            return {"text": f"{n} of {total} {word}", "tone": tone}
    if total and all(item["verdict"] == "pass" for item in items):
        return {"text": "Passed" if total == 1 else f"All {total} passed", "tone": "good"}
    return {"text": "No verdict", "tone": "muted"}


def _row(item: dict[str, Any], frames: list[dict[str, Any]], base: str) -> dict[str, Any]:
    icon, tone, word = RESULTS[item["verdict"]]
    note = f"{word} at step {item['at']}" if item["at"] is not None else word
    if item["verdict"] == "none":
        note = "No verdict"
    severity = item["severity"]
    steps = [_step(step, len(item["steps"]), frames, base) for step in item["steps"]]
    first = next((i for i, s in enumerate(item["steps"]) if s["n"] == item["at"]), 0)
    return {
        "key": item["item"],
        "name": item["item"],
        "icon": icon,
        "tone": tone,
        "note": note,
        "severity": {"text": severity.title(), "tone": SEVERITY_TONES[severity]} if severity else None,
        "first": first,
        "steps": steps,
    }


def _step(step: dict[str, Any], count: int, frames: list[dict[str, Any]], base: str) -> dict[str, Any]:
    icon, tone, word = RESULTS[step["result"]]
    label = step["name"] or f"Step {step['n']}"
    beats = [_beat(b, frames, base) for b in step["beats"]]
    finding = step["finding"]
    still = step["still"]
    return {
        "n": step["n"],
        "label": label,
        "icon": icon,
        "tone": tone,
        "card": f"Step {step['n']} of {count} · {label}",
        "head": f"Step {step['n']} · {label}" + ("" if beats else ": didn't run"),
        "expected": step["expected"] or "unknown: the scenario is not in the tested build",
        "got": step["got"] or word,
        "hint": f"{finding['severity'].title()} finding: {finding['text']}" if finding else None,
        "still": base + still if still and (_FRAME.match(still) or _SHOT.match(still)) else None,
        "rough": sum(1 for b in step["beats"] if b["k"] == "miss") > 1,
        "beats": beats,
    }


def _size_at(frames: list[dict[str, Any]], t: float) -> tuple[int, int]:
    """The screen's size in page pixels at second ``t``: the last frame shown by then."""
    shown = frames[0]
    for frame in frames:
        if frame["t"] > t:
            break
        shown = frame
    return int(shown["w"]), int(shown["h"])


def _box(box: list[float], w: int, h: int) -> list[float]:
    return [
        round(box[0] / w * 100, 2),
        round(box[1] / h * 100, 2),
        round(box[2] / w * 100, 2),
        round(box[3] / h * 100, 2),
    ]


def _beat(beat: dict[str, Any], frames: list[dict[str, Any]], base: str) -> dict[str, Any]:
    kind = beat["k"]
    text = str(beat.get("text", ""))
    out: dict[str, Any] = {"k": kind, "icon": BEAT_ICONS.get(kind, "eye"), "bad": kind == "miss"}
    if kind == "think":
        return out | {"log": text, "say": text}
    if kind == "type":
        typed = text + (" ↵" if beat.get("enter") else "")
        # The tag types the text out a key at a time: what it says after each key.
        keys = [f"Typing “{text[:n]}”" for n in range(len(text) + 1)]
        out |= {"log": f"Typed “{typed}”", "say": f"Typed “{typed}”", "keys": keys}
    elif kind == "shot":
        out |= {"log": "Took a screenshot", "say": "Took a screenshot"}
    else:
        out |= {"log": text, "say": text}
    out |= {"t": beat["t"], "seg": beat["seg"]}
    w, h = _size_at(frames, beat["seg"][0])
    if "x" in beat and "y" in beat:
        out |= {"x": round(beat["x"] / w * 100, 2), "y": round(beat["y"] / h * 100, 2)}
    if "down" in beat:
        out["down"] = beat["down"]
    if beat.get("outer"):
        out["outer"] = _box(beat["outer"], w, h)
    if beat.get("inner"):
        out["inner"] = [_box(b, w, h) for b in beat["inner"]]
    return out


def cleaned(snap: Snap, at: str) -> str:
    keep = snap.settings.get_int("qa.keep_bundles")
    return f"Cleaned up {day_label(at[:10])}: QA is kept for the newest {keep} merge bundles."


def tester_runs(task_id: str, head: str, evidence: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Each tester kind's runs on ``head``, one per tester of the kind, as the addresses of their replay pages."""
    runs: dict[str, list[str]] = {}
    for e in evidence:
        if e["head"] == head and not e["cleaned"]:
            runs.setdefault(str(e["tester"]), []).append(url(task_id, head, str(e["tester"]), str(e["run"])))
    return runs


def shots(of: str, entry: dict[str, Any]) -> list[str]:
    """The addresses of a run's screenshots; none once the clean-up removed them."""
    base = evidence_base(of, str(entry["head"]), str(entry["tester"]), str(entry["run"]))
    return [] if entry["cleaned"] else [base + f for f in entry["shots"]]
