"""One line on how the factory runs: paused or not, the merges waiting for their check, staging's last probe, the
testers at work, the orchestrator's state and the plan's weekly use, as the warden reports them.

On the server, as its admin: `python3 -I pulse.py`. The Monitor watch prints the line only when it changes.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from runtime.cli.factory import Call, ask
from runtime.shared.paths import Places

WANT = ["factory", "vitals", "probes", "sessions", "plan"]


def line(state: dict[str, Any]) -> str:
    paused = state["factory"]["paused"]
    merges = (state["vitals"]["reading"] or {}).get("merges", {}).get("value", "unknown")
    probes = state["probes"]["recent"].get("staging") or []
    staging = f"{probes[-1]['commit']}{'' if probes[-1]['ok'] else ' DOWN'}" if probes else "unknown"
    live = [s for s in state["sessions"].values() if s.get("state") in ("working", "waiting")]
    testers = sum(1 for s in live if str(s.get("role", "")).startswith("qa-"))
    lead = next((s["state"] for s in state["sessions"].values() if s.get("role") == "orchestrator"
                 and s.get("state") != "retired"), "none")  # fmt: skip
    week = state["plan"]["seven_day"]
    plan = f"{week['used_percentage']}%" if week and week.get("used_percentage") is not None else state["plan"]["state"]
    run = f"PAUSED: {paused.get('why') or 'by the owner'}" if paused else "running"
    return (f"{run} | merges: {merges} | staging {staging} | "
            f"testers {testers} | orchestrator {lead} | week {plan}")  # fmt: skip


answer = ask(Places.system().owner_socket, Call("snapshot", {"want": WANT}))
print(line(answer["state"]) if answer.get("ok") else f"pulse: refused: {json.dumps(answer)[:300]}")
