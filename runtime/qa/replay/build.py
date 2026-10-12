"""A tester's run made into its replay, after the run, by code alone: the same run always gives the same replay.

In: the run's folder in the evidence (its trace and screenshots), the tester's transcript, its step marks and
results, its findings, and the scenarios it walked. Out, in the folder's ``replay/``: ``replay.json``, the frames
it plays, and nothing of the trace besides. The frames are the trace's own pictures, so the replay's clock is
Chrome's by construction. A secret the tester typed or was handed (a sign-in link, the desktop's password) never
reaches the replay: its words say what it was, and from the moment it was typed until ``replay.secret_seconds``
after the typing ended the picture is hidden: those frames and screenshots are deleted. The trace, which holds what
was typed and every address, is deleted once the replay is built.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from runtime.qa import qa_browser
from runtime.qa.replay import beats as beats_mod
from runtime.qa.replay import trace as trace_mod
from runtime.qa.replay.timeline import Timings, order
from runtime.qa.scenarios import Scenario, Step
from runtime.qa.wrapper_tools import LINK
from runtime.shared.models import SEVERITIES

FOLDER = "replay"
DATA = "replay.json"
VERSION = 1
# Text the run's folder may hold that names an address; any of it that names a secret is deleted with the trace.
_TEXTS = (".yml", ".yaml", ".md", ".log", ".json", ".txt")
Mask = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class Inputs:
    run: Path
    transcript: Path
    records: list[dict[str, Any]]  # the tester's step marks and results (steps.py)
    findings: list[dict[str, Any]]  # the tester's findings (findings.py)
    scenario: Callable[[str], Scenario | None]  # a standing scenario by name, as the tested head holds it
    timings: Timings
    recorder: tuple[float, float]  # replay.recorder_before_seconds, replay.recorder_after_seconds
    shares: beats_mod.Shares
    secret_seconds: float


def build(given: Inputs) -> dict[str, Any]:
    """Build the run's replay into its folder and return what was written."""
    trace = trace_mod.read(given.run, *given.recorder)
    t0 = trace.frames[0].at
    said = beats_mod.read(given.transcript)
    secrets = said.secrets | _kept(given.run)
    moments = [{"k": "think", "text": text, "at": round(at - t0, 2)} for at, text in said.thoughts]
    last: list[tuple[int, int]] = []
    for use in sorted(said.uses, key=lambda u: u.at):
        found = beats_mod.beat(use, trace, t0, last, given.shares)
        if found is not None:
            moments.append(found)
    mask = _masker(secrets)
    hidden = _mask(moments, secrets, mask, given.secret_seconds)
    _drop_hidden_shots(given.run, moments, hidden)
    items = _items(given, moments, t0, mask)
    frames = _frames(given.run, trace.frames, t0, hidden)
    for item in items:
        for step in item["steps"]:
            step["still"] = _still(given.run, step, frames)
    data = {
        "version": VERSION,
        "frames": frames,
        "hidden": hidden,
        "items": items,
        "seconds": round(trace.frames[-1].at - t0, 2),
    }
    _scrub(given.run, _forms(secrets))
    (given.run / FOLDER / DATA).write_text(json.dumps(data), encoding="utf-8")
    return data


def failed(run: Path, reason: str) -> None:
    """A run whose replay could not be built says why, where its replay would be, and keeps no trace nor secret."""
    (run / FOLDER).mkdir(exist_ok=True)
    try:
        secrets: set[str] | None = _forms(_kept(run))
    # telemetry: expected — secrets that cannot be read only mean every text file of the run goes
    except trace_mod.ReplayError:
        secrets = None
    _scrub(run, secrets)
    (run / FOLDER / DATA).write_text(json.dumps({"version": VERSION, "error": reason}), encoding="utf-8")


def _kept(run: Path) -> set[str]:
    path = run / qa_browser.SECRETS
    if not path.is_file():
        return set()
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise trace_mod.ReplayError("the run's kept sign-in links cannot be read, so its secrets are unknown") from exc
    if not isinstance(kept, list):
        raise trace_mod.ReplayError("the run's kept sign-in links are not a list, so its secrets are unknown")
    return {str(s) for s in kept}


def _forms(secrets: set[str]) -> set[str]:
    """Each secret as it may be written: whole, and a link also as a person reads it (its host and path) when its
    path holds the secret, as a magic link's does."""
    found = set(secrets)
    found |= {beats_mod.short(s) for s in secrets if LINK.fullmatch(s) and urlparse(s).path not in ("", "/")}
    return {s for s in found if s}


def _masker(secrets: set[str]) -> Mask:
    """Every form of every secret in a text becomes what it was: the sign-in link, or the desktop's password."""
    said = {
        form: "the sign-in link" if LINK.fullmatch(s) else "the desktop's password"
        for s in secrets
        for form in _forms({s})
    }
    longest_first = sorted(said, key=len, reverse=True)

    def mask(text: str) -> str:
        for form in longest_first:
            text = text.replace(form, said[form])
        return text

    return mask


def _mask(moments: list[dict[str, Any]], secrets: set[str], mask: Mask, seconds: float) -> list[list[float]]:
    """Mask every beat's words; each time a secret was typed opens a hidden window, from when the call was sent (it
    may have waited behind another) to ``seconds`` after it ended."""
    hidden: list[list[float]] = []
    for moment in moments:
        text = str(moment.get("text", ""))
        masked = mask(text)
        if masked == text:
            continue
        moment["text"] = masked
        if moment["k"] == "type":
            hidden.append([moment["t"], round(moment["seg"][1] + seconds, 2)])
    return hidden


def _inside(t: float, hidden: list[list[float]]) -> bool:
    return any(start <= t <= end for start, end in hidden)


def _drop_hidden_shots(run: Path, moments: list[dict[str, Any]], hidden: list[list[float]]) -> None:
    """A screenshot taken while a secret could be on screen is deleted, and its beat names no picture."""
    for moment in moments:
        if moment["k"] == "shot" and moment.get("file") and _inside(moment["seg"][1], hidden):
            shot = run / str(moment.pop("file"))
            if shot.parent == run and not shot.is_symlink():
                shot.unlink(missing_ok=True)


Key = tuple[str, int]  # an item and a step's number


def _marks(records: list[dict[str, Any]], t0: float) -> tuple[list[tuple[float, str, int]], dict[Key, dict[str, Any]]]:
    """Each step's start on the replay's clock, in order, and each step's last result."""
    starts: list[tuple[float, str, int]] = []
    results: dict[Key, dict[str, Any]] = {}
    for record in records:
        at = beats_mod.wall(record.get("at"))
        key = (str(record.get("item")), int(record.get("step", 0)))
        if at is None:
            continue
        if "result" in record:
            results[key] = record
        else:
            starts.append((round(at - t0, 2), *key))
    return sorted(starts), results


def _items(given: Inputs, moments: list[dict[str, Any]], t0: float, mask: Mask) -> list[dict[str, Any]]:
    """The run's items in the order the tester began them, each step with its beats, words and result."""
    starts, results = _marks(given.records, t0)
    moments = sorted(moments, key=lambda m: m["at"] if m["k"] == "think" else m["seg"][0])
    bounds = _bounds(starts, moments)
    grouped: dict[Key, list[dict[str, Any]]] = {}
    started = {(item, n) for _, item, n in starts}
    for moment in moments:
        when = moment["at"] if moment["k"] == "think" else moment["seg"][0]
        owner = next((key for begin, key in reversed(bounds) if when >= begin), None)
        if owner is not None:
            grouped.setdefault(owner, []).append(moment)
    items: dict[str, dict[str, Any]] = {}
    for _, begun, _n in starts:
        items.setdefault(begun, {"item": begun, "steps": []})
    for name, item in items.items():
        scenario = given.scenario(name)
        # A step the tester marked past the scenario's last still shows, unnamed.
        count = max([len(scenario.steps) if scenario else 0, *(n for _, i, n in starts if i == name)])
        for n in range(1, count + 1):
            written = scenario.steps[n - 1] if scenario and n <= len(scenario.steps) else None
            key = (name, n)
            step = _step(key, written, grouped.get(key, []), results.get(key), key in started, given, mask)
            item["steps"].append(step)
        item |= _verdict(item["steps"], [f for f in given.findings if str(f.get("step", "")).startswith(name + ":")])
    return list(items.values())


def _bounds(starts: list[tuple[float, str, int]], moments: list[dict[str, Any]]) -> list[tuple[float, Key]]:
    """Where each step begins: its mark, moved back to the thought the tester wrote just before marking it, so the
    step opens with what the tester said it would do."""
    bounds = []
    for at, item, n in starts:
        begin = at
        for moment in moments:
            when = moment["at"] if moment["k"] == "think" else moment["seg"][0]
            if when > at:
                break
            begin = when if moment["k"] == "think" else at
        bounds.append((min(begin, at), (item, n)))
    return bounds


def _step(
    key: Key,
    written: Step | None,
    moments: list[dict[str, Any]],
    result: dict[str, Any] | None,
    started: bool,
    given: Inputs,
    mask: Mask,
) -> dict[str, Any]:
    item, n = key
    findings = [f for f in given.findings if f.get("step") == f"{item}:{n}"]
    worst = min(findings, key=lambda f: SEVERITIES.index(str(f["severity"]))) if findings else None
    said = mask(str(worst["text"])).split("\n", 1)[0][:300] if worst else ""
    return {
        "n": n,
        "name": written.name if written else "",
        "expected": written.expected if written else "",
        "result": str(result["result"]) if result else ("none" if started else "skip"),
        "got": mask(str(result["got"])) if result else "",
        "finding": {"severity": worst["severity"], "text": said} if worst else None,
        "beats": order(moments, given.timings),
    }


def _verdict(steps: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    severities = [str(f["severity"]) for f in findings]
    worst = next((s for s in SEVERITIES if s in severities), None)
    for result in ("fail", "blocked"):
        at = next((s["n"] for s in steps if s["result"] == result), None)
        if at is not None:
            return {"verdict": result, "at": at, "severity": worst}
    ran = [s for s in steps if s["result"] != "skip"]
    whole = bool(ran) and all(s["result"] == "pass" for s in ran)
    return {"verdict": "pass" if whole else "none", "at": None, "severity": worst}


def _frames(run: Path, frames: list[trace_mod.Frame], t0: float, hidden: list[list[float]]) -> list[dict[str, Any]]:
    """The frames the replay plays, moved out of the trace and numbered; a frame shown while a secret could be on
    screen is not kept."""
    target = run / FOLDER / "frames"
    target.mkdir(parents=True, exist_ok=True)
    resources = run / "traces" / "resources"
    kept: list[dict[str, Any]] = []
    for frame in frames:
        t = round(frame.at - t0, 3)
        source = resources / frame.file
        if _inside(t, hidden) or not source.is_file() or source.is_symlink():
            continue
        name = f"{len(kept) + 1:06d}.jpg"
        shutil.copyfile(source, target / name)
        kept.append({"t": t, "file": f"{FOLDER}/frames/{name}", "w": frame.width, "h": frame.height})
    return kept


def _still(run: Path, step: dict[str, Any], frames: list[dict[str, Any]]) -> str | None:
    """The step's picture: the tester's last screenshot in it, else the frame at its end; None for a step not run."""
    shots = [b["file"] for b in step["beats"] if b.get("k") == "shot" and b.get("file")]
    if shots and (run / shots[-1]).is_file():
        return str(shots[-1])
    ends = [b["seg"][1] for b in step["beats"] if "seg" in b]
    if not ends or not frames:
        return None
    return str(max((f for f in frames if f["t"] <= max(ends)), key=lambda f: f["t"], default=frames[0])["file"])


def _scrub(run: Path, secrets: set[str] | None) -> None:
    """Delete the trace, the kept secrets, and every text file in the run that names a secret; every text file,
    when the secrets are unknown."""
    shutil.rmtree(run / "traces", ignore_errors=True)
    (run / qa_browser.SECRETS).unlink(missing_ok=True)
    for folder, _, names in os.walk(run):
        for name in names:
            path = Path(folder) / name
            if path.suffix in _TEXTS and path.name != DATA and not path.is_symlink():
                text = path.read_text(encoding="utf-8", errors="replace")
                if secrets is None or any(secret in text for secret in secrets):
                    path.unlink()
