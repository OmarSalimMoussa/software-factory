"""Install a pushed commit on the server, then bring the agents back: `python3 install.py <full sha>`, from the laptop.

factory.upgrade holds new testers and waits for running ones itself. This asks it, follows the warden's `upgrade` part
until the commit is installed, and resumes the factory, unless the factory was paused before: that pause is the
owner's, or the warden's at the plan limit, and only the owner lifts it. An upgrade the warden refuses while
sessions settle, or one that fails, is asked again a few times.

Needs FACTORY_SSH, the server's admin login, and FACTORY_SCRIPTS, this folder in the server's install.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any

SSH, SCRIPTS = os.environ["FACTORY_SSH"], os.environ["FACTORY_SCRIPTS"]
ATTEMPTS, RETRY_SECONDS, POLL_SECONDS, RESUME_TRIES = 5, 60, 20, 12


def owner(verb: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """The warden's answer to an owner verb, or None while it does not answer, as during the install itself."""
    remote = f"python3 -I {shlex.quote(SCRIPTS + '/owner_call.py')} {shlex.quote(verb)} {shlex.quote(json.dumps(args))}"
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH, remote],
                              capture_output=True, text=True, timeout=120)  # fmt: skip
        answer: dict[str, Any] = json.loads(done.stdout)
    # telemetry: expected — the warden is down while it installs; the caller says so and asks again
    except (subprocess.TimeoutExpired, ValueError):
        return None
    return answer


def upgrade() -> dict[str, Any] | None:
    answer = owner("snapshot", {"want": ["upgrade"]})
    return answer["state"]["upgrade"] if answer and answer.get("ok") else None


def say(text: str) -> None:
    print(f"{time.strftime('%H:%M', time.gmtime())} {text}", flush=True)


def follow(sha: str) -> bool:
    """Whether the commit got installed, saying each change of the upgrade's progress."""
    last = ""
    while True:
        seen = upgrade()
        progress = (seen or {}).get("progress") or {}
        now = "the warden does not answer" if seen is None else " ".join(
            str(progress.get(k) or "") for k in ("state", "step", "problem")).strip() or "asked"  # fmt: skip
        if now != last:
            say(now)
            last = now
        if seen is not None and seen["installed"] == sha:
            return True
        if progress.get("to") == sha and progress.get("state") == "failed":
            return False
        time.sleep(POLL_SECONDS)


def main(sha: str) -> int:
    before = upgrade()
    if before is None:
        say("the warden does not answer: try again once it does")
        return 1
    if before["paused"]:
        say("the factory was paused before this install, and stays paused for the owner")
    for _ in range(ATTEMPTS):
        asked = owner("factory.upgrade", {"commit": sha})
        say(json.dumps(asked))
        if asked and asked.get("ok") and follow(sha):
            break
        time.sleep(RETRY_SECONDS)
    else:
        say(f"gave up after {ATTEMPTS} attempts")
        return 1
    if before["paused"]:
        return 0
    for _ in range(RESUME_TRIES):
        back = owner("factory.resume", {})
        say(json.dumps(back))
        if back and back.get("ok"):
            return 0
        time.sleep(POLL_SECONDS)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
