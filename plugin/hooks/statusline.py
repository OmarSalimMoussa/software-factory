"""The orchestrator's status line, which also keeps the plan's usage windows where the warden reads them.

Claude Code runs it after each reply with the session's status as JSON on stdin. When that JSON carries
``rate_limits``, the 5-hour and 7-day windows are written, with the time this script wrote them, to
``~/.cache/<the factory's install folder name>/plan.json`` (or ``--out``). The warden ages the reading by
``written_at``, never by the file's own times. A status line must never break the session: any failure prints a
fallback line and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

WINDOWS = ("five_hour", "seven_day")
_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def default_out() -> Path:
    # The cache folder is named after the install folder (this file sits in <install>/plugin/hooks/), so the warden
    # finds the reading from the same name without either side spelling it.
    return Path.home() / ".cache" / Path(__file__).resolve().parents[2].name / "plan.json"


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def window(raw: object) -> dict[str, Any] | None:
    """One window as {used_percentage, resets_at}, or None when Claude Code did not send a readable one."""
    if not isinstance(raw, dict):
        return None
    used, resets = raw.get("used_percentage"), raw.get("resets_at")
    if not _number(used) or not _number(resets):
        return None
    return {"used_percentage": used, "resets_at": resets}


def reading(status: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """What plan.json should hold for this status, or None when the status says nothing about the plan."""
    limits = status.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = {name: window(limits.get(name)) for name in WINDOWS}
    if all(w is None for w in windows.values()):
        return None
    return {**windows, "written_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_atomically(path: Path, record: dict[str, Any]) -> None:
    """Replace ``path`` in one step, so the warden never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".plan-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def line(status: dict[str, Any], record: dict[str, Any] | None) -> str:
    model = status.get("model")
    parts = [model["display_name"]] if isinstance(model, dict) and isinstance(model.get("display_name"), str) else []
    if record is None:
        return " · ".join([*parts, "plan limits not read yet"])
    for name in WINDOWS:
        win = record[name]
        parts.append(f"{_LABELS[name]} {win['used_percentage']:g}%" if win else f"{_LABELS[name]} unknown")
    return " · ".join(parts)


def run(stdin: TextIO, stdout: TextIO, out: Path, now: datetime) -> None:
    status = json.loads(stdin.read())
    if not isinstance(status, dict):
        raise ValueError("the status line input is not an object")
    record = reading(status, now)
    if record is not None:
        write_atomically(out, record)
    print(line(status, record), file=stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="where to write the reading (default: ~/.cache/<install>/plan.json)")
    args = parser.parse_args(argv)
    try:
        run(sys.stdin, sys.stdout, args.out or default_out(), datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - a status line never fails the session; the line names the failure
        print(f"plan limits unknown ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
