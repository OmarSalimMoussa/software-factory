"""Install the factory from one commit of the owner's clone, as root:

    sudo python3 runtime/system/install.py --commit <sha> --repo <product checkout> [--healthchecks] [--set k=v]...

Add ``--dry-run`` (no sudo needed) to print every step and what it would do, changing nothing. The install archives
the commit, tests that copy, and places it read-only; sets up the users, the units, polkit, the firewall, Claude Code's
managed settings and the sandbox's needs; starts the factory; and runs the drills. It refuses to begin while any
session is working or waiting, and refuses to finish while any drill fails. Rerun it after every change to the factory.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared.paths import Places  # noqa: E402 - the path line above must run first
from runtime.shared.protocol import Refused  # noqa: E402
from runtime.system import drills, healthchecks, steps_machine, steps_prepare, steps_wire  # noqa: E402
from runtime.system.host import CommandError, DryHost, Host, RealHost  # noqa: E402
from runtime.system.job import REPO_ROOT, Install, refuse  # noqa: E402


def run_drills(ctx: Install) -> None:
    if ctx.host.dry:
        for drill in drills.DRILLS:
            ctx.host.say(f"  would drill: {drill.__name__}")
        return
    from runtime.system import drill_rig

    with drill_rig.opened(ctx) as rig:
        results = drills.run_all(rig)
    for result in results:
        ctx.host.say(f"  {'pass' if result.ok else 'FAIL'}: {result.name}: {result.evidence}")
    ctx.drilled = [{"name": r.name, "ok": r.ok, "evidence": r.evidence} for r in results]
    failed = [r.name for r in results if not r.ok]
    if failed:
        raise refuse(f"{len(failed)} drill(s) failed ({'; '.join(failed)}): the install is not recorded, so the "
                     "warden starts nothing; fix what each says, then install again")  # fmt: skip


def record(ctx: Install) -> None:
    steps_wire.record(ctx, ctx.drilled)


STEPS: tuple[tuple[str, Callable[[Install], None]], ...] = (
    ("Check the machine", steps_prepare.check_machine),
    ("Refuse while a session is working or waiting", steps_prepare.refuse_live_sessions),
    ("Archive the commit", steps_prepare.archive),
    ("Test the archived copy", steps_prepare.test_copy),
    ("Settle the settings", steps_prepare.settle_settings),
    ("Install the packages", steps_machine.packages),
    ("Create the users", steps_machine.users),
    ("Stop the running factory", steps_prepare.stop_factory),
    ("Lay out the state folder", steps_machine.state_tree),
    ("Place the factory, read-only", steps_machine.place),
    ("Install Claude Code for the agents", steps_machine.claude_binary),
    ("Let bubblewrap run", steps_machine.apparmor),
    ("Give the agent user its checkout", steps_machine.agent_checkout),
    ("Give the warden its clone of the factory", steps_machine.warden_clone),
    ("Set up the stack user's rootless Docker", steps_machine.rootless_docker),
    ("Keep the secrets", steps_machine.secrets),
    ("Write Claude Code's managed settings", steps_wire.managed_settings),
    ("Install the units", steps_wire.units),
    ("Write the polkit rule", steps_wire.polkit),
    ("Write the firewall", steps_wire.firewall),
    ("Install the factory command", steps_wire.factory_command),
    ("Point both product checkouts at the factory's git hooks", steps_wire.git_hooks),
    ("Publish the dashboard on the tailnet", steps_wire.tailnet),
    ("Start the factory", steps_wire.start),
    ("Run the drills", run_drills),
    ("Create the Healthchecks.io checks", healthchecks.create),
    ("Record the install", record),
)


def run(ctx: Install) -> None:
    for number, (title, step) in enumerate(STEPS, 1):
        ctx.host.say(f"{number:>2}. {title}")
        step(ctx)


def parse_changes(items: list[str]) -> dict[str, Any]:
    """--set key=value: a TOML value (a number, a list, a quoted string), or else the text as written."""
    changes: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise refuse(f"--set {item}: write it as key=value")
        try:
            changes[key.strip()] = tomllib.loads(f"value = {raw}")["value"]
        except tomllib.TOMLDecodeError:
            changes[key.strip()] = raw
    return changes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="install.py", description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True, help="the factory commit to install")
    parser.add_argument("--repo", required=True, type=Path, help="your checkout of the product the factory works on")
    parser.add_argument("--healthchecks", action="store_true", help="create the Healthchecks.io checks")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="a setting to save")
    parser.add_argument("--dry-run", action="store_true", help="print every step; change nothing")
    args = parser.parse_args(argv)
    if not args.dry_run and (os.geteuid() != 0 or "SUDO_USER" not in os.environ):
        print("install.py: run it with sudo, as the owner (or add --dry-run)", file=sys.stderr)
        return 2
    host: Host = DryHost() if args.dry_run else RealHost()
    owner = RealHost().account(os.environ.get("SUDO_USER") or getpass.getuser())
    if owner is None:
        print("install.py: the owner's account cannot be read", file=sys.stderr)
        return 2
    try:
        ctx = Install(host, Places.system(), REPO_ROOT, args.repo.resolve(), args.commit, owner,
                      parse_changes(args.set), args.healthchecks)  # fmt: skip
        run(ctx)
    except Refused as exc:
        print(f"install.py: refused: {exc.reason}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
