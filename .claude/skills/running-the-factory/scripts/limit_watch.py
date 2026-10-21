"""Print each Claude usage-limit error an agent met in the last ten minutes that this watch has not printed before,
marked WEEKLY when it names the weekly limit; remember what it printed, in /root/.limit-watch.json.

On the server, with sudo, as the transcripts are the agents': `sudo python3 -I limit_watch.py`. The warden pauses the
factory itself at `cost.plan_pause_percent` of the weekly plan; this catches the limits the plan reading does not show.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from runtime.warden.sessions import launch

SEEN = Path("/root/.limit-watch.json")
LIMIT = re.compile(r"limit reached|usage limit|weekly limit|out of extra usage|hit your .{0,20}limit", re.I)
WEEKLY = re.compile(r"weekly|week|7.day", re.I)
RECENT_SECONDS = 600
TAIL_BYTES = 65536


def errors(path: Path) -> list[dict[str, object]]:
    """The API error lines near the end of one transcript."""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    found = []
    for raw in lines:
        if "isApiErrorMessage" in raw or "<synthetic>" in raw:
            try:
                found.append(json.loads(raw))
            # telemetry: expected — the tail starts mid-line, and that cut line is no JSON
            except ValueError:
                continue
    return found


def text_of(entry: dict[str, object]) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    return " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))


seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
since = time.time() - RECENT_SECONDS
for path in launch.transcripts(launch.agent_home()).rglob("*.jsonl"):
    try:
        recent = path.stat().st_mtime >= since and errors(path)
    # telemetry: expected — a transcript removed between the walk and the read has nothing to say
    except OSError:
        continue
    for entry in recent or []:
        said, key = text_of(entry), str(entry.get("uuid") or f"{path}:{entry.get('timestamp')}")
        if LIMIT.search(said) and key not in seen:
            seen.add(key)
            kind = "WEEKLY" if WEEKLY.search(said) else "limit"
            print(f"{kind} {str(entry.get('timestamp', ''))[:16]} {path.stem[:20]}: {said.strip()[:200]}")
SEEN.write_text(json.dumps(sorted(seen)[-500:]))
