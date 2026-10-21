"""Print each Self-reflect entry, and each unresolved issue in the factory's own GlitchTip project, that this watch
has not printed before; then remember what it printed, in ~/.factory-watch.json.

On the server, as its admin: `python3 -I watch.py`. The GlitchTip keys are the admin's copy of the set-up's
server.json, in ~/.config/software-factory/. A source that does not answer is said in one line, never skipped quietly.
"""

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from runtime.cli.factory import Call, ask
from runtime.shared import error_reports
from runtime.shared.paths import Places
from runtime.system import layout
from runtime.warden.health import telemetry

SEEN = Path.home() / ".factory-watch.json"
KEYS = Path.home() / ".config" / "software-factory" / layout.SERVER_KEYS


def reflections(after: str) -> tuple[list[dict[str, Any]], str]:
    """The entries newer than ``after``, oldest first, and the newest time seen."""
    answer = ask(Places.system().owner_socket, Call("snapshot", {"want": ["reflections"]}))
    rows = sorted(answer["state"]["reflections"], key=lambda r: r["at"])
    fresh = [r for r in rows if r["at"] > after]
    return fresh, max([after, *(r["at"] for r in fresh)])


def issues() -> list[dict[str, Any]]:
    keys = telemetry.read_keys(KEYS, "glitchtip", ("url", "org", "token"), f"{KEYS} is missing")
    service = telemetry.GlitchTip(keys["url"], keys["org"], keys["token"])
    url = f"{service.url}/api/0/projects/{service.org}/{error_reports.PROJECT}/issues/?query=is:unresolved"
    request = urllib.request.Request(url, headers=telemetry.bearer(service))
    with urllib.request.urlopen(request, timeout=15) as answer:
        found: list[dict[str, Any]] = json.load(answer)
    return found


seen = json.loads(SEEN.read_text()) if SEEN.exists() else {"reflections": "", "issues": []}
try:
    fresh, seen["reflections"] = reflections(seen["reflections"])
    for row in fresh:
        print(f"reflect {row['at'][:16]} {row['kind']} {row['id']}: {row['text'][:400]}")
# telemetry: surfaced — `print` says the warden did not answer, and why
except (OSError, ValueError, KeyError) as exc:
    print(f"watch: the warden did not answer ({type(exc).__name__})")
try:
    for issue in issues():
        if issue["id"] not in seen["issues"]:
            print(f"error {issue['id']} x{issue.get('count')}: {(issue.get('title') or '(no title)')[:300]}")
            seen["issues"].append(issue["id"])
# telemetry: surfaced — `print` says GlitchTip did not answer, and why
except (OSError, ValueError, KeyError, telemetry.TelemetryError) as exc:
    print(f"watch: GlitchTip did not answer ({type(exc).__name__})")
SEEN.write_text(json.dumps(seen))
