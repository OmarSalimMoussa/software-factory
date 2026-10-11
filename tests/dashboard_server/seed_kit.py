"""What the seed's parts share: where the fixtures are, the models and prices the afternoon uses, and a fixture's
"5 h ago" or "Oct 9, 14:02" turned into a time.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


FIXTURES = ROOT / "testdata" / "dashboard-fixtures"


MODELS = {"Opus 5.5": "claude-opus-5-5", "Sonnet 5.5": "claude-sonnet-5-5", "Haiku 5.5": "claude-haiku-5-5"}


PRICES = {"claude-opus-5-5": (15, 75), "claude-sonnet-5-5": (3, 15), "claude-haiku-5-5": (1, 5)}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(text: str) -> float:
    """A fixture's "5 h ago" as seconds."""
    if text in ("just now", "0 min ago"):
        return 60.0
    if text == "yesterday":
        return 30 * 3600.0
    n = int(re.match(r"\d+", text).group(0)) if re.match(r"\d+", text) else 1  # type: ignore[union-attr]
    return n * {"min": 60, "h": 3600, "days": 86400}[text.split()[1]]


def fixture_time(text: str, now: float) -> float:
    """A fixture's "Oct 9, 14:02" as a time on or before ``now``."""
    stamp = datetime.strptime(f"{datetime.fromtimestamp(now, UTC).year} {text}", "%Y %b %d, %H:%M")
    return min(stamp.replace(tzinfo=UTC).timestamp(), now - 60)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_picture(repo: Path, task_id: str, source: str) -> str:
    data = (FIXTURES / "pictures" / source).read_bytes()
    name = f"{task_id}-{hashlib.sha256(data).hexdigest()[:12]}.svg"
    write(repo / "queue" / "pictures" / name, data.decode())
    return f"pictures/{name}"
