"""Install the factory from one commit of the owner's clone, as root:

    sudo python3 runtime/system/install.py --commit <sha> --repo <product checkout> [--healthchecks] [--set k=v]...

Add ``--dry-run`` (no sudo needed) to print every step and what it would do, changing nothing. The install runs from a
sealed, read-only copy of the commit and takes only a commit the factory's CI passed: nothing is tested on this
machine. It places that copy; sets up the users, the units, polkit, the firewall, Claude Code's managed settings and
the sandbox's needs; starts the shared services every slot uses, then the factory; and runs the drills. It refuses to
begin while any session is working or waiting, and refuses to finish while any drill fails. Rerun it after every
change to the factory.
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
from runtime.system import (  # noqa: E402
    ci_check,
    drills,
    healthchecks,
    layout,
    sealed,
    steps_machine,
    steps_prepare,
    steps_services,
    steps_toolchain,
    steps_wire,
)
from runtime.system.host import Account, CommandError, DryHost, Host, RealHost  # noqa: E402
from runtime.system.job import REPO_ROOT, Install, refuse  # noqa: E402

PROGRAM = "install.py"


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
    ("Run from a sealed copy of the commit", steps_prepare.sealed_copy),
    ("Check that the factory's CI passed on the commit", ci_check.passed),
    ("Settle the settings", steps_prepare.settle_settings),
    ("Install the packages", steps_machine.packages),
    ("Create the users", steps_machine.users),
    ("Stop the running factory", steps_prepare.stop_factory),
    ("Lay out the state folder", steps_machine.state_tree),
    ("Place the factory, read-only", steps_machine.place),
    ("Install Claude Code for the agents", steps_machine.claude_binary),
    ("Install the product's toolchain", steps_toolchain.toolchain),
    ("Let bubblewrap run", steps_machine.apparmor),
    ("Give the agent user its checkout", steps_machine.agent_checkout),
    ("Give the warden its clone of the factory", steps_machine.warden_clone),
    ("Set up the stack user's rootless Docker", steps_machine.rootless_docker),
    ("Place the shared services", steps_services.place),
    ("Keep the secrets", steps_machine.secrets),
    ("Write Claude Code's managed settings", steps_wire.managed_settings),
    ("Install the units", steps_wire.units),
    ("Write the polkit rule", steps_wire.polkit),
    ("Write the firewall", steps_wire.firewall),
    ("Install the factory command", steps_wire.factory_command),
    ("Point both product checkouts at the factory's git hooks", steps_wire.git_hooks),
    ("Publish the dashboard on the tailnet", steps_wire.tailnet),
    ("Start the shared services", steps_services.start),
    ("Keep the shared services' keys", steps_services.keys),
    ("Start the factory", steps_wire.start),
    ("Run the drills", run_drills),
    ("Create the Healthchecks.io checks", healthchecks.create),
    ("Record the install", record),
)


# The steps before this one change nothing on the machine, so a failure there leaves nothing to take back.
FIRST_CHANGE = next(number for number, (_, step) in enumerate(STEPS, 1) if step is steps_machine.packages)


def run(ctx: Install) -> None:
    for number, (title, step) in enumerate(STEPS, 1):
        ctx.reached = number
        ctx.host.step(number, title)
        step(ctx)


def job(host: Host, places: Places, owner: Account, repo: Path, commit: str, changes: dict[str, Any],
        healthchecks: bool) -> Install:  # fmt: skip
    # Run from the sealed copy (from_commit), the owner's checkout is named by the process that sealed it.
    checkout = Path(os.environ.get(sealed.CHECKOUT, str(REPO_ROOT)))
    ctx = Install(host, places, checkout, repo.resolve(), commit, owner, changes, healthchecks)
    if sealed.STAGE in os.environ:
        ctx.stage, ctx.sha = Path(os.environ[sealed.STAGE]), os.environ[sealed.SHA]
    # Known before any step runs: only a first install is ever taken back.
    ctx.first = not host.exists(layout.install_record(places.state))
    return ctx


def perform(ctx: Install, *, keep: bool) -> str | None:
    """Every step, as the command line and the console both run them. A failure is told through the host, then a
    failed first install is taken back; returns what stopped it, or None once the install finished."""
    try:
        run(ctx)
    except (Refused, CommandError, Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, Refused):
            problem = f"refused: {exc.reason}"
        elif isinstance(exc, CommandError):
            problem = str(exc)
        else:
            problem = f"stopped at step {ctx.reached}: {type(exc).__name__}: {exc}"
        ctx.host.fail(PROGRAM, problem)
        if rolls_back(ctx, keep=keep):
            roll_back(ctx)
        return problem
    return None


def rolls_back(ctx: Install, *, keep: bool) -> bool:
    """Only a real first install that got as far as changing the machine, unless the owner keeps it to look at."""
    return not ctx.host.dry and ctx.first and ctx.reached >= FIRST_CHANGE and not keep


def roll_back(ctx: Install) -> None:
    """A first install that failed takes back everything it made, so the next one starts from a clean machine. A
    failed reinstall never does: removing it would delete the agents' clone, their queue and every stored secret."""
    from runtime.system import uninstall

    ctx.host.say("The install failed before it finished, so everything it made is removed again:")
    uninstall.remove_all(ctx.host, ctx.places, ctx.owner.name, ctx.product)
    ctx.host.say("The machine is as it was before this install. Fix what it said, then install again.")


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


def from_commit(commit: str, owner: str, argv: list[str]) -> int:
    """Seal ``commit`` and run the install again from that copy, so every step is the commit's own code: nothing in
    the checkout's working files (an edit in progress, a file not committed) can change what an install does."""
    try:
        sha, stage = sealed.stage_commit(RealHost(), commit, owner, REPO_ROOT)
    except (Refused, CommandError) as exc:
        print(f"{PROGRAM}: {exc.reason if isinstance(exc, Refused) else exc}", file=sys.stderr)
        return 1
    print(f"Installing {sha[:12]}: every step runs from that commit, sealed read-only in {stage}", flush=True)
    sealed.exec_staged(sha, stage, "runtime/system/install.py", argv, checkout=REPO_ROOT)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True, help="the factory commit to install")
    parser.add_argument("--repo", required=True, type=Path, help="your checkout of the product the factory works on")
    parser.add_argument("--healthchecks", action="store_true", help="create the Healthchecks.io checks")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="a setting to save")
    parser.add_argument("--dry-run", action="store_true", help="print every step; change nothing")
    keep = "leave a failed first install in place, to look at it, instead of removing it"
    parser.add_argument("--keep-on-failure", action="store_true", help=keep)
    args = parser.parse_args(argv)
    if not args.dry_run and (os.geteuid() != 0 or "SUDO_USER" not in os.environ):
        print("install.py: run it with sudo, as the owner (or add --dry-run)", file=sys.stderr)
        return 2
    host: Host = DryHost() if args.dry_run else RealHost()
    owner = RealHost().account(os.environ.get("SUDO_USER") or getpass.getuser())
    if owner is None:
        print("install.py: the owner's account cannot be read", file=sys.stderr)
        return 2
    if not args.dry_run and sealed.STAGE not in os.environ:
        return from_commit(args.commit, owner.name, argv)
    ctx = job(host, Places.system(), owner, args.repo, args.commit, parse_changes(args.set), args.healthchecks)
    problem: str | None = "interrupted"
    try:
        problem = perform(ctx, keep=args.keep_on_failure)
    finally:
        sealed.finish(host, ctx.stage, kept=problem is not None and args.keep_on_failure)
    return 0 if problem is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
