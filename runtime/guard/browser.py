"""The guard's rules for driving the product and for what the owner alone sees.

`product-drivers`: only the UI testers drive the product, in their own browser or on the remote desktop; no session
drives the owner's Chrome. `subagents-at-once`: a lane's browser is its first tester's while it runs.
`qa-browser`: no code runs in the browser server itself, a browser call names no file, a route answers what it
catches itself, and a text wait gives no `time`, which sleeps before it and is no time-out.
`walked-scenario`: a regression tester reads no scenario in the checkout, which holds the tip's text.
`qa-evidence`: no agent opens the factory's state (the testers' recordings, screenshots and saved sign-ins) or the
dashboard. The evidence rule reads paths and addresses, never prose: a report may name a screenshot.
"""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from runtime.guard import shell, transcript
from runtime.guard.facts import Call, Decision, Refusal, Rewrite, SessionView, inside, public, resolve
from runtime.qa.scenarios import FOLDER
from runtime.qa.tool_list import WAIT_UNTIL
from runtime.qa.wrapper import FILENAME, ROUTE, SERVER_CODE, WAIT_FOR
from runtime.shared import lanes
from runtime.shared.models import WORKING
from runtime.warden.verbs import REGRESSION

BROWSER = f"mcp__{lanes.BROWSER}__"
CHROME = "mcp__claude-in-chrome__"
# The remote desktop is a video in the tester's browser, so a click at a point is a desktop step; a web page is
# driven by its element references.
DESKTOP = ("mcp__computer-use__", "mcp__remote-devices__computer", BROWSER + "browser_mouse_")
DRIVERS = (BROWSER, *DESKTOP)
# Each browser wait the guard cuts to a tester's time left: the input that sets its length, and seconds per unit.
_WAITS = {BROWSER + WAIT_FOR: ("time", 1), BROWSER + WAIT_UNTIL: ("minutes", 60)}
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
# The input fields that name a file or an address, tool by tool; Grep's `pattern` is text, Glob's a path.
_PATH_FIELDS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path", "glob"),
    "LS": ("path",),
    "WebFetch": ("url",),
}
_BROWSER_FIELDS = ("paths", "url")


def check(call: Call) -> Decision:
    for rule in (_drivers, _held, _evidence, _server, _walked):
        decision = rule(call)
        if decision is not None:
            return decision
    return None


def _drivers(call: Call) -> Decision:
    if call.off("product-drivers"):
        return None
    if call.tool.startswith(CHROME):
        return Refusal("product-drivers", "no session drives the owner's Chrome; a UI tester uses its own browser")
    personas = call.settings.get_list("subagents.browser_personas")
    if call.tool.startswith(DRIVERS) and not (call.in_subagent and call.agent_type in personas):
        return Refusal("product-drivers", "only UI testers drive the product; write code and tests instead")
    return None


def _held(call: Call) -> Decision:
    """Claude Code runs one browser server for a lane's testers: the first that wrote within qa.stale_seconds has it."""
    me = call.sessions.get(call.agent_id) if call.in_subagent and call.tool.startswith(BROWSER) else None
    if me is None or call.off("subagents-at-once"):
        return None
    holders = [s for s in call.sessions.values() if s.parent == me.parent and s.started_as == me.started_as
               and s.state == WORKING and (s.started, s.id) < (me.started, me.id) and _lively(call, s)]  # fmt: skip
    if not holders:
        return None
    first = f"this browser is the {me.started_as} tester's started before you, which still runs"
    return Refusal("subagents-at-once", f"{first}: hand back now, untested, and say you need a lane of your own")


def _lively(call: Call, s: SessionView) -> bool:
    path = transcript.subagent_transcript(call.transcript, s.agent_id)
    try:
        last = datetime.fromtimestamp(path.stat().st_mtime, tz=call.now.tzinfo)
    # telemetry: expected — a tester that has written nothing yet counts from its start
    except OSError:
        last = datetime.fromisoformat(s.started.replace("Z", "+00:00"))
    return (call.now - last).total_seconds() < call.settings.get_int("qa.stale_seconds")


def _evidence(call: Call) -> Decision:
    if call.off("qa-evidence") or not any(_owners(call, word) for word in _named(call)):
        return None
    return Refusal(
        "qa-evidence",
        "the factory's state, the testers' recordings, screenshots and sign-ins, and the dashboard are the owner's; "
        "report from what your tools answered",
    )


def _server(call: Call) -> Decision:
    if not call.tool.startswith(BROWSER) or call.off("qa-browser"):
        return None
    if call.tool.removeprefix(BROWSER) == SERVER_CODE:
        return Refusal("qa-browser", "code in the browser server runs outside the page; run a page script instead")
    # A named file lands wherever the name says; without one it lands in the run's own evidence folder.
    if FILENAME in call.tool_input:
        return Refusal("qa-browser", "name no file: the browser names it, and its answer says the name to report")
    # A route with neither sends the request on, past the routes that keep the owner's dashboard out of reach.
    if call.tool == BROWSER + ROUTE and call.tool_input.get("status") is None and call.tool_input.get("body") is None:
        return Refusal("qa-browser", "a route answers what it catches itself: give it the status the step asks for")
    # A text wait's time sleeps its whole length before the wait starts, and is no time-out: it goes, and the text
    # wait gives up after a few seconds on its own.
    if call.tool == BROWSER + WAIT_FOR and "time" in call.tool_input and {"text", "textGone"} & call.tool_input.keys():
        return Rewrite({k: v for k, v in call.tool_input.items() if k != "time"})
    return None


def _walked(call: Call) -> Decision:
    """A bundle walks slot 0 at the commit its merges reached, which a merge since may have changed a scenario after."""
    if not (call.in_subagent and lanes.kind_of(call.agent_type) == REGRESSION) or call.off("walked-scenario"):
        return None
    folder = call.worktree / FOLDER
    # An address names a page, not a file, unless it is a file:// one; a tester's folder may be the scenarios' own.
    local = [w.removeprefix("file://") for w in _named(call) if "://" not in w.removeprefix("file://")]
    words = [word for word in local if "/" in word or word.startswith(("~", "."))]
    found = next((p for p in (resolve(w, call.cwd, call.home) for w in words) if inside(p, folder)), None)
    if found is None:
        return None
    return Refusal(
        "walked-scenario",
        f"{found} is in this checkout's scenarios, which hold the default branch's tip, and the build you walk may be "
        "older: read each with `factory qa scenario <name>`, as that build has it",
    )


def cap_wait(call: Call, left: float) -> Decision:
    """A tester's browser wait, Playwright's or the factory's, cut to the seconds it has left."""
    found = _WAITS.get(call.tool)
    if found is None:
        return None
    name, unit = found
    asked = call.tool_input.get(name)
    if isinstance(asked, bool) or not isinstance(asked, int | float) or asked * unit <= left:
        return None
    seconds = max(1, int(left))
    return Rewrite({**call.tool_input, name: seconds if unit == 1 else seconds / unit})


def _named(call: Call) -> list[str]:
    """The words of a call that name a file or an address: a command's words and redirects, or a tool's path
    fields."""
    if call.tool in {"Bash", "Monitor"}:
        try:
            found = shell.invocations(str(call.tool_input.get("command", "")))
        # telemetry: expected — an unreadable command has no words for this rule; the shell rules refuse it
        except shell.ShellError:
            return []
        return [word for inv in found for word in (*inv.argv, *inv.writes, *inv.reads)]
    fields = _BROWSER_FIELDS if call.tool.startswith(BROWSER) else _PATH_FIELDS.get(call.tool, ())
    words: list[str] = []
    for name in fields:
        value = call.tool_input.get(name)
        words += [str(v) for v in value] if isinstance(value, list) else [str(value or "")]
    return words


def _owners(call: Call, word: str) -> bool:
    """Whether a word names the owner's record: the dashboard's address, or a path into the factory's state (with a
    slash, or from `~` or `.`, so a search for a folder's name is not one), or a wildcard reaching into it."""
    if not word or any(c.isspace() for c in word):
        return False
    host, port = _address(word)
    hosts = call.settings.get_list("dashboard.hosts")
    if host in hosts and (port == call.settings.get_int("dashboard.port") or host not in _LOOPBACK):
        return True
    for candidate in [word, word.split("=", 1)[1] if "=" in word else ""]:
        if "/" not in candidate and not candidate.startswith(("~", ".")):
            continue
        path = resolve(candidate, call.cwd, call.home)
        state = call.places.state
        if (inside(path, state) and not inside(path, public(call.places))) or _reaches(path, state):
            return True
    return False


def _reaches(pattern: Path, folder: Path) -> bool:
    """Whether a wildcard could match a path inside ``folder`` other than its public part."""
    if not any(c in str(pattern) for c in "*?["):
        return False
    parts, inner = PurePosixPath(pattern).parts, folder.parts
    for given, real in zip(parts, inner, strict=False):
        if given == "**":
            return True
        if not fnmatchcase(real, given):
            return False
    return len(parts) > len(inner) and parts[len(inner)] != "public"


def _address(word: str) -> tuple[str, int | None]:
    """The host and port a word names, as `scheme://host[:port]/...` or a bare `host[:port]/...`."""
    rest = word.split("://", 1)[-1].split("@", 1)[-1]
    hostport = rest.split("/", 1)[0]
    host, _, port = hostport.partition(":")
    return host.lower(), int(port) if port.isdigit() else None
