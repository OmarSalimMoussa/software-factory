"""software-stack-run@<id>.service: one stack request as the stack user, with its rootless Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/stack_run.py <state> <id>``. A product verb runs the
checkout's own ``scripts/dev`` with its slot's facts in the environment. Three verbs are the factory's and never reach
the product: sweeping a slot's labelled Docker objects, the nightly prune, and Docker's disk use. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared.paths import RUN, Places  # noqa: E402 - the path line above must run first
from runtime.slots import leases  # noqa: E402
from runtime.system.runners import launch_file as lf  # noqa: E402

SLOT_LABEL = "software.slot"
# docker system df prints go-units sizes: decimal steps of 1000, such as 0B, 512kB, 1.25GB.
_SIZE = re.compile(r"^([\d.]+)\s*([kKMGTPEZY]?)B$")
_STEPS = "kmgtpezy"
# Each kind of Docker object a slot labels, with the commands that list and remove it.
_KINDS = (
    (["container", "ls", "-aq"], ["container", "rm", "-f"]),
    (["volume", "ls", "-q"], ["volume", "rm", "-f"]),
    (["network", "ls", "-q"], ["network", "rm"]),
    (["image", "ls", "-q"], ["image", "rm", "-f"]),
)


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def sweep(slot: int, timeout: int) -> int:
    failed = 0
    for listing, removal in _KINDS:
        found = _docker([*listing, "--filter", f"label={SLOT_LABEL}={slot}"], timeout)
        ids = sorted(set(found.stdout.split()))
        if found.returncode != 0:
            print(f"sweep: listing {listing[0]}s failed: {found.stderr.strip()[-300:]}", flush=True)
            failed += 1
        elif ids:
            done = _docker([*removal, *ids], timeout)
            print(f"sweep: removed {len(ids)} {listing[0]}(s) of slot {slot}", flush=True)
            failed += done.returncode != 0
    return 1 if failed else 0


def prune(hours: str, timeout: int) -> int:
    if not hours.isdigit() or int(hours) < 1:
        print("prune: name the age in whole hours", flush=True)
        return lf.REFUSED
    status = 0
    for args in (["builder", "prune", "-f"], ["image", "prune", "-a", "-f"]):
        done = _docker([*args, "--filter", f"until={hours}h"], timeout)
        print(done.stdout.strip() or done.stderr.strip(), flush=True)
        status |= done.returncode
    return 1 if status else 0


def size_bytes(text: str) -> int:
    found = _SIZE.match(text.strip())
    if found is None:
        raise ValueError(f"{text!r} is not a size")
    unit = found.group(2).lower()
    step = _STEPS.index(unit) + 1 if unit else 0
    return round(float(found.group(1)) * float(1000**step))


def disk(timeout: int) -> int:
    done = _docker(["system", "df", "--format", "{{json .}}"], timeout)
    if done.returncode != 0:
        print(f"disk: docker system df failed: {done.stderr.strip()[-300:]}", flush=True)
        return 1
    try:
        total = sum(size_bytes(json.loads(line)["Size"]) for line in done.stdout.splitlines() if line.strip())
    except (ValueError, KeyError, TypeError) as exc:
        print(f"disk: docker system df printed something unreadable ({exc})", flush=True)
        return 1
    print(total, flush=True)
    return 0


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        raw = lf.read(state, request_id, (lf.STACK,))
        settings, sessions = lf.published(state)
        slot, verb, session = lf.stack_parts(raw, settings)
        timeout = settings.get_int("slots.verb_minutes") * 60
        if verb[0] == lf.SWEEP:
            return sweep(slot, timeout)
        if verb[0] == lf.PRUNE:
            return prune(verb[1] if len(verb) == 2 else "", timeout)
        if verb[0] == lf.DISK:
            return disk(timeout)
        checkout = lf.checkout_for(session, settings, sessions)
    except lf.BadRequest as exc:
        print(f"stack-run: refused: {exc}", flush=True)
        return lf.REFUSED
    except subprocess.TimeoutExpired:
        print("stack-run: docker did not answer in time", flush=True)
        return lf.TIMED_OUT
    places = Places(Path(__file__).resolve().parents[3], state, RUN)
    env = {**os.environ, **leases.env(places, settings, slot)}
    return lf.run([str(checkout / "scripts" / "dev"), *verb], checkout, env, timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
