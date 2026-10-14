"""The guard's rules for driving the product and for what the owner alone sees.

`product-drivers`: only the UI testers drive the product, in their own browser or on the remote desktop; no session
drives the owner's Chrome. `qa-browser`: no code runs in the browser server itself, and a browser call names no file.
`qa-evidence`: no agent opens the factory's state (the testers' recordings, screenshots and saved sign-ins) or the
dashboard. The evidence rule reads paths and addresses, never prose: a report may name a screenshot.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from runtime.guard import shell
from runtime.guard.facts import Call, Decision, Refusal, Rewrite, inside, public, resolve
from runtime.qa.wrapper import SERVER_CODE
from runtime.qa.wrapper_tools import WAIT_UNTIL
from runtime.shared import lanes

BROWSER = f"mcp__{lanes.BROWSER}__"
CHROME = "mcp__claude-in-chrome__"
# The remote desktop is a video in the tester's browser, so a click at a point is a desktop step; a web page is
# driven by its element references.
DESKTOP = ("mcp__computer-use__", "mcp__remote-devices__computer", BROWSER + "browser_mouse_")
DRIVERS = (BROWSER, *DESKTOP)
WAIT = "browser_wait_for"
# Each browser wait the guard cuts to a tester's time left: the input that sets its length, and seconds per unit.
_WAITS = {BROWSER + WAIT: ("time", 1), BROWSER + WAIT_UNTIL: ("minutes", 60)}
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
    for rule in (_drivers, _evidence, _server):
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
    if "filename" in call.tool_input:
        return Rewrite({k: v for k, v in call.tool_input.items() if k != "filename"})
    return None


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
