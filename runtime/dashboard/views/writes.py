"""Every action the dashboard sends: the warden verb it asks for, its arguments, the version it must still match,
the toast it answers with and the parts of the page it reloads.

A sheet's action carries the version the owner saw; when the thing changed since, it is refused with "This changed;
here it is now." and nothing is asked of the warden. A move carries the order the device saw, checked the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.dashboard.views import settings
from runtime.dashboard.views.agent import agent_version
from runtime.dashboard.views.kit import PartError, Snap, plural
from runtime.dashboard.views.tasks import proposals
from runtime.dashboard.views.words import taskwords

STALE = "This changed; here it is now."
Body = dict[str, Any]
Args = Callable[[Body, str, "Snap | None"], dict[str, Any]]
Check = Callable[[Body, str, Snap], None]
Data = Callable[[Body], bytes]


class Stale(Exception):
    """What the owner acted on changed since the device drew it."""


@dataclass(frozen=True, slots=True)
class Write:
    verb: str
    args: Args
    message: Callable[[dict[str, Any], Snap | None], str]
    refresh: list[str]
    check: Check | None = None
    wants: tuple[str, ...] = ()
    settle: str = ""
    data: Data | None = None  # bytes the warden takes apart from the arguments, such as a token


def _text(body: Body, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PartError(f"Send {key}.")
    return value.strip()


def _brief_comment(body: Body, task: str) -> dict[str, Any]:
    """The text the owner selected in one version, a little of what comes before and after it, and their note."""
    shown, around = body.get("version"), [body.get("prefix"), body.get("suffix")]
    if isinstance(shown, bool) or not isinstance(shown, int) or not all(isinstance(a, str) for a in around):
        raise PartError("Select text in the brief first.")
    return {"task": task, "version": shown, "quote": _text(body, "quote"), "prefix": around[0], "suffix": around[1],
            "note": _text(body, "note")}  # fmt: skip


def _direction(body: Body) -> str:
    if body.get("dir") not in (-1, 1):
        raise PartError("dir is -1 to move up or 1 to move down.")
    return "up" if body["dir"] == -1 else "down"


def _seen(body: Body) -> list[str]:
    seen = body.get("seen_order")
    if not isinstance(seen, list) or not all(isinstance(t, str) for t in seen):
        raise PartError("Send the order you saw as seen_order.")
    return seen


def _version(body: Body) -> str:
    """The version of the thing the owner saw, which every action on one carries: an action on what changed since
    is refused, never applied to what the owner did not see."""
    sent = body.get("version")
    if not isinstance(sent, str) or not sent:
        raise Stale(STALE)
    return sent


def _card_check(body: Body, ref: str, snap: Snap) -> None:
    sent = _version(body)
    tasks = taskwords.tasks(snap)
    if ref not in tasks or tasks[ref]["status"] != "proposed" or proposals.card_version(snap, ref) != sent:
        raise Stale(STALE)


def _need_check(body: Body, ref: str, snap: Snap) -> None:
    sent = _version(body)
    card = next((c for c in snap.get("cards") if c["id"] == ref), None)
    if card is None or proposals.need_version(card) != sent:
        raise Stale(STALE)


def _agent_check(body: Body, ref: str, snap: Snap) -> None:
    sent = _version(body)
    row = snap.get("sessions").get(ref)
    if row is None or agent_version(row) != sent:
        raise Stale(STALE)


def _proposal_move(body: Body, ref: str, snap: Snap | None) -> dict[str, Any]:
    """The warden checks the order with its own placeholders for empty places; the device saw only the cards."""
    assert snap is not None
    order = snap.get("proposals")["open"]
    if [t for t in order if t and t != "none"] != _seen(body):
        raise Stale(STALE)
    return {"task": ref, "direction": _direction(body), "seen": order}


def _reason(body: Body) -> str:
    """Why the owner dropped it; the panel's Drop sends none, and the warden needs one for the task's requests."""
    reason = body.get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else "dropped by the owner from the dashboard"


def _run(body: Body) -> int:
    raw = body.get("run")
    if not isinstance(raw, str | int) or not str(raw).isdigit():
        raise PartError("Choose a run from the list.")
    return int(raw)


def _settings_args(body: Body, ref: str, snap: Snap | None) -> dict[str, Any]:
    assert snap is not None
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise PartError("Send the changed settings.")
    saved = {key: settings.to_saved(snap.settings, key, value) for key, value in changes.items()}
    return {"changes": saved}


def _push_back_args(body: Body, ref: str, snap: Snap | None) -> dict[str, Any]:
    """Push back names the build prod ran before, as the push sheet showed it (short); the warden is sent that build
    in full, and refuses if prod's previous build has moved on since."""
    assert snap is not None
    sent = _text(body, "expected_sha")
    previous = snap.get("deploys")["push_back"]
    if not previous or not previous.startswith(sent):
        raise Stale(STALE)
    return {"expected_sha": previous}


def _yolo_args(body: Body, ref: str, snap: Snap | None) -> dict[str, Any]:
    on = body.get("on")
    if not isinstance(on, bool):
        raise PartError("Say whether YOLO mode is on.")
    return {"on": on}


def _account_name(body: Body) -> str:
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PartError("Name the account.")
    return name.strip()


def _account_token(body: Body) -> bytes:
    """The token, as bytes for the warden alone; the warden says what is wrong with one."""
    token = body.get("token")
    if not isinstance(token, str) or not token.strip():
        raise PartError("Paste the account's token.")
    return token.strip().encode()


def _said(answer: dict[str, Any], fallback: str) -> str:
    message = answer.get("message")
    return message if isinstance(message, str) and message else fallback


def _said_text(body: Body) -> str:
    """The message as typed; the warden says what is wrong with it (empty, too long)."""
    text = body.get("text")
    if not isinstance(text, str):
        raise PartError("Write the message first.")
    return text


def _stopped(answer: dict[str, Any]) -> str:
    return f"Stopped {plural(len(answer.get('retired', [])), 'agent')}."


def _needs(action: str, verb: str, args: Args) -> tuple[re.Pattern[str], Write]:
    return _w(
        f"needs/{_P}/{action}",
        verb,
        args,
        lambda a, s: proposals.done_words(s, action) if s else "Done.",
        ["home", "work"],
        _need_check,
        ("cards",),
        action,
    )


def _w(
    path: str,
    verb: str,
    args: Args,
    message: Callable[[dict[str, Any], Snap | None], str],
    refresh: list[str],
    check: Check | None = None,
    wants: tuple[str, ...] = (),
    settle: str = "",
    data: Data | None = None,
) -> tuple[re.Pattern[str], Write]:
    return re.compile(f"^/api/{path}$"), Write(verb, args, message, refresh, check, wants, settle, data)


_P = r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
_HOME = ["home", "work", "shell"]
_CARDS = ("proposals", "queue")
# fmt: off
WRITES: list[tuple[re.Pattern[str], Write]] = [
    _w(f"proposals/{_P}/approve", "proposal.approve", lambda b, r, s: {"task": r},
       lambda a, s: "Approved. It starts when a slot is free.", _HOME, _card_check, _CARDS, "approve"),
    _w(f"proposals/{_P}/later", "proposal.later", lambda b, r, s: {"task": r},
       lambda a, s: "Not now. It goes back to the queue.", _HOME, _card_check, _CARDS, "later"),
    _w(f"proposals/{_P}/drop", "proposal.drop", lambda b, r, s: {"task": r, "reason": _reason(b)},
       lambda a, s: "Dropped.", _HOME, _card_check, _CARDS),
    _w(f"proposals/{_P}/move", "proposal.move", _proposal_move, lambda a, s: "Moved.", _HOME, _card_check, _CARDS),
    _w(f"proposals/{_P}/run", "proposal.run", lambda b, r, s: {"task": r, "run": _run(b)},
       lambda a, s: f"The run is now rung {a['run']}.", _HOME, _card_check, _CARDS),
    _w(f"approved/{_P}/move", "approved.move", lambda b, r, s: {"task": r, "direction": _direction(b),
                                                                 "seen": _seen(b)}, lambda a, s: "Moved.", _HOME),
    _w(f"tasks/{_P}/boost", "task.boost", lambda b, r, s: {"task": r},
       lambda a, s: "Boosted. It is the next proposal.", _HOME),
    _w(f"tasks/{_P}/unboost", "task.unboost", lambda b, r, s: {"task": r}, lambda a, s: "Unboosted.", _HOME),
    _w(f"requests/{_P}/boost", "request.boost", lambda b, r, s: {"request": r},
       lambda a, s: "Boosted. Triage takes it next, on its own.", ["work"]),
    _w(f"parked/{_P}/unpark", "parked.unpark", lambda b, r, s: {"item": r}, lambda a, s: "Back in the queue.", _HOME),
    _needs("go", "needs.go", lambda b, r, s: {"card": r}),
    _w(f"needs/{_P}/feedback", "needs.feedback", lambda b, r, s: {"card": r},
       lambda a, s: "The designer is reading your comments.", ["home"], _need_check, ("cards",)),
    _w(f"brief/{_P}/comment", "brief.comment", lambda b, r, s: _brief_comment(b, r), lambda a, s: "Comment saved.",
       ["brief", "home"]),
    _w(f"brief/{_P}/resolve", "brief.resolve", lambda b, r, s: {"task": r, "thread": _text(b, "thread")},
       lambda a, s: "Resolved.", ["brief", "home"]),
    _needs("extend", "needs.extend", lambda b, r, s: {"card": r}),
    _needs("designer", "needs.designer", lambda b, r, s: {"card": r}),
    _needs("park", "needs.park", lambda b, r, s: {"card": r}),
    _needs("answer", "needs.answer", lambda b, r, s: {"card": r, "answer": _text(b, "text")}),
    _needs("fixed", "needs.fixed", lambda b, r, s: {"card": r}),
    _w(f"agents/{_P}/stop", "agent.stop", lambda b, r, s: {"session": r},
       lambda a, s: _stopped(a), ["agents", "home", "shell"], _agent_check, ("sessions",)),
    _w("agent/message", "agent.message", lambda b, r, s: {"session": _text(b, "session"), "text": _said_text(b)},
       lambda a, s: _said(a, "Sending your message."), ["agent", "home"]),
    _w("outage/stop", "outage.stop", lambda b, r, s: {}, lambda a, s: _said(a, "The fix is stopped."), _HOME),
    _w("orchestrator/start", "orchestrator.start", lambda b, r, s: {},
       lambda a, s: "The orchestrator is starting.", ["agents", "shell"]),
    _w("factory/pause", "factory.pause", lambda b, r, s: {},
       lambda a, s: "Paused. Every agent is benched with its work kept.", ["agents", "home", "shell"]),
    _w("factory/resume", "factory.resume", lambda b, r, s: {},
       lambda a, s: "Resuming. Every agent comes back in order.", ["agents", "home", "shell"]),
    _w("settings", "settings.save", _settings_args, lambda a, s: "Saving.", ["settings"], wants=("settings",)),
    _w("settings/repo", "repo.change", lambda b, r, s: {"changes": {"repo.path": _text(b, "path")}},
       lambda a, s: "Changing the repo.", ["settings"]),
    _w(f"rules/{_P}", "rule.switch", lambda b, r, s: {"rule": r, "on": b.get("on")},
       lambda a, s: f"{a['rule']} is {'on' if a['on'] else 'off'}.", ["guard"]),
    _w("push", "push.prod", lambda b, r, s: {"expected_staging_sha": _text(b, "expected_staging_sha")},
       lambda a, s: _said(a, "Pushing to prod."), ["home", "shell"]),
    _w("push/back", "push.back", _push_back_args, lambda a, s: _said(a, "Pushing prod back."), ["home", "shell"],
       wants=("deploys",)),
    _w("slot/open", "slot.open", lambda b, r, s: {}, lambda a, s: "Opening slot 0, signed in.", []),
    _w("staging/push", "staging.push", lambda b, r, s: {"sha": _text(b, "sha")},
       lambda a, s: _said(a, "Pushing to staging."), ["home", "shell"]),
    _w("yolo", "yolo.switch", _yolo_args, lambda a, s: "YOLO mode is on." if a["on"] else "YOLO mode is off.",
       ["home", "settings", "shell"]),
    _w("accounts", "account.add", lambda b, r, s: {"name": _account_name(b), "switch": b.get("switch") is True},
       lambda a, s: _said(a, "Added."),
       ["settings"], data=_account_token),
    _w(f"accounts/{_P}/use", "account.use", lambda b, r, s: {"name": r}, lambda a, s: _said(a, "Switching."),
       ["settings", "agents", "shell"]),
    _w(f"accounts/{_P}/remove", "account.remove", lambda b, r, s: {"name": r}, lambda a, s: _said(a, "Removed."),
       ["settings"]),
]
# fmt: on


def find(path: str) -> tuple[Write, str] | None:
    for pattern, write in WRITES:
        match = pattern.match(path)
        if match:
            return write, match.group(1) if match.groups() else ""
    return None
