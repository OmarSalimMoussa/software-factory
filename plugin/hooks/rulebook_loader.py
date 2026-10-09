"""SessionStart and SubagentStart hook: adds the rulebook and the self-reflect note where they belong.

Managed settings run it on both events for every session on the machine, the owner's included, as
``python3 -I <install>/plugin/hooks/rulebook_loader.py --org <github owner> [--org …] --slice <unit>``.

- ``rules/engineering.md`` goes to a session or subagent whose repo has a remote owned by one of the ``--org``
  owners, and to no other.
- ``plugin/self-reflect.md`` goes to every factory session at its start: a process inside the factory's systemd
  slice. A subagent gets it from the guard, appended to its brief.

Standard library only. A missing rules file fails the hook loudly; a folder that is not a repo, or has no remote,
simply gets no rulebook.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

INSTALL = Path(__file__).resolve().parents[2]
ENGINEERING = INSTALL / "rules" / "engineering.md"
PATTERNS = INSTALL / "rules" / "patterns.md"
SELF_REFLECT = INSTALL / "plugin" / "self-reflect.md"
_GIT_SECONDS = 5
EVENTS = ("SessionStart", "SubagentStart")


def remote_owner(url: str) -> str | None:
    """The owner (first path segment) of a remote URL, or None for a local path."""
    if "://" in url:
        parts = urlsplit(url)
        if not parts.netloc:
            return None
        path = parts.path
    elif ":" in url and not url.startswith("/") and "/" not in url.split(":", 1)[0]:
        path = url.split(":", 1)[1]  # scp-like: [user@]host:owner/name
    else:
        return None
    segments = [s for s in path.split("/") if s]
    return segments[0] if len(segments) >= 2 else None


def remote_urls(cwd: str) -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    done = subprocess.run(
        [git, "-C", cwd, "config", "--get-regexp", r"^remote\..*\.url$"],
        capture_output=True,
        text=True,
        timeout=_GIT_SECONDS,
        check=False,
    )
    # Exit 1 means no remote; 128 means not a repo. Either way the session gets no rulebook.
    if done.returncode != 0:
        return []
    return [row.split(None, 1)[1] for row in done.stdout.splitlines() if len(row.split(None, 1)) == 2]


def in_org_repo(urls: list[str], orgs: list[str]) -> bool:
    wanted = {org.lower() for org in orgs}
    return any((owner := remote_owner(url)) is not None and owner.lower() in wanted for url in urls)


def in_slice(cgroup_text: str, slice_name: str) -> bool:
    return any(f"/{slice_name}/" in row + "/" for row in cgroup_text.splitlines())


def context(urls: list[str], orgs: list[str], self_reflect: bool) -> str:
    parts = []
    if in_org_repo(urls, orgs):
        parts.append(ENGINEERING.read_text(encoding="utf-8"))
        parts.append(f"The generic patterns are in {PATTERNS}: read them before building something new.")
    if self_reflect:
        parts.append(SELF_REFLECT.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", action="append", required=True, help="a GitHub owner whose repos get the rulebook")
    parser.add_argument("--slice", required=True, help="the systemd slice every factory session runs in")
    args = parser.parse_args(argv)
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("the hook input is not an object")
    event, cwd = payload.get("hook_event_name"), payload.get("cwd")
    if event not in EVENTS or not isinstance(cwd, str):
        raise ValueError(f"the hook input names no cwd, or an event other than {' or '.join(EVENTS)}")
    cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    self_reflect = event == "SessionStart" and in_slice(cgroup, args.slice)
    text = context(remote_urls(cwd), args.org, self_reflect)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
