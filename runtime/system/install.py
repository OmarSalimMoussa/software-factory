"""Install the factory from one commit, as root. The release file runs it (``sudo python3 software-factory.pyz
--terminal --repo <product checkout>``); from the owner's clone of the factory it is

    sudo python3 runtime/system/install.py --commit <sha> --repo <product checkout> [--healthchecks] [--set k=v]...

Add ``--dry-run`` (no sudo needed) to go through every step, reading this machine and changing nothing, and
``--verbose`` to print every detail the log keeps. The install runs from a sealed, read-only copy of the commit and
takes only a commit the factory's CI passed: nothing is tested on this machine. It shows a few phases; every command
and its output goes to a log in /var/log/software-factory. It refuses to begin while any session is working or
waiting. Rerun it after every change to the factory.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tomllib
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Run by its path, which puts no folder of the factory's on sys.path: its root goes first, for the imports below.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared import environment, store, upgrades
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import ci_check, healthchecks, layout, release, sealed
from runtime.system.host import Account, CommandError, Host
from runtime.system.install_steps import (
    steps_machine,
    steps_prepare,
    steps_secrets,
    steps_services,
    steps_toolchain,
    steps_wire,
)
from runtime.system.job import REPO_ROOT, Install, refuse
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost
from runtime.system.unattended import UnattendedHost
from runtime.system.uninstall import LookingHost

PROGRAM = "install.py"


@dataclass(frozen=True)
class Phase:
    """A few steps the owner sees as one line: its title, then what each step is doing."""

    title: str
    steps: tuple[tuple[str, Callable[[Install], None]], ...]


PHASES: tuple[Phase, ...] = (
    Phase("Checking this computer", (
        ("Checking the tools this computer needs", steps_prepare.check_machine),
        ("Checking that no agent is at work", steps_prepare.refuse_live_sessions),
        ("Checking the copy the install runs from", steps_prepare.sealed_copy),
        ("Checking that the factory's own checks passed", ci_check.passed),
        ("Checking the settings", steps_prepare.settle_settings),
    )),
    Phase("Preparing this computer", (
        ("Installing the system packages", steps_machine.packages),
        ("Creating the factory's users", steps_machine.users),
        ("Stopping the running factory", steps_prepare.stop_factory),
        ("Making the factory's folders", steps_machine.state_tree),
    )),
    # Downloading the services takes longest on a first install, so it starts here and runs in the background
    # while the owner answers the questions and the factory is set up.
    Phase("Fetching the services", (
        ("Setting up the services' own Docker", steps_machine.rootless_docker),
        ("Placing the services", steps_services.place),
        ("Downloading the services", steps_services.pull),
    )),
    Phase("Connecting your accounts", (
        ("Setting up your keys and accounts", steps_secrets.secrets),
        ("Setting up the Healthchecks.io checks", healthchecks.create),
    )),
    Phase("Setting up the factory", (
        ("Copying the factory into place", steps_machine.place),
        ("Giving the agents their copy of the product", steps_machine.agent_checkout),
        ("Reading GitHub's host keys for the warden", steps_machine.github_host_keys),
        ("Installing Claude Code for the agents", steps_machine.claude_binary),
        ("Copying your tools for the agents", steps_toolchain.toolchain),
        ("Letting the checks' walls run", steps_machine.apparmor),
        ("Writing Claude Code's settings for the agents", steps_wire.managed_settings),
        ("Writing the factory's services", steps_wire.units),
        ("Writing the factory's permissions", steps_wire.polkit),
        ("Writing the factory's firewall", steps_wire.firewall),
        ("Installing the factory commands", steps_wire.factory_command),
        ("Adding the factory's checks to the product", steps_wire.git_hooks),
        ("Moving the product to this factory", steps_wire.product_pin),
        ("Publishing the dashboard on your tailnet", steps_wire.tailnet),
    )),
    Phase("Starting up", (
        ("Starting the services", steps_services.start),
        ("Connecting the services", steps_services.keys),
        ("Starting the factory", steps_wire.start),
    )),
    Phase("Finishing", (
        ("Recording the install", steps_wire.record),
    )),
)  # fmt: skip
STEPS = tuple(step for phase in PHASES for step in phase.steps)


# The steps before this one change nothing on the machine, so a failure there leaves nothing to take back.
FIRST_CHANGE = next(number for number, (_, step) in enumerate(STEPS, 1) if step is steps_machine.packages)


def run(ctx: Install) -> None:
    number = 0
    for index, phase in enumerate(PHASES, 1):
        ctx.host.phase(index, phase.title)
        for title, step in phase.steps:
            number += 1
            ctx.reached, ctx.doing = number, title
            ctx.host.step(number, title)
            step(ctx)
    ctx.host.done()


def job(host: Host, places: Places, owner: Account, repo: Path, commit: str, changes: dict[str, Any],
        healthchecks: bool) -> Install:  # fmt: skip
    source = environment.staged()
    factory = REPO_ROOT if source is None else source.checkout
    ctx = Install(host, places, factory, repo.resolve(), commit, owner, changes, healthchecks)
    if source is not None:
        ctx.stage, ctx.sha = source.stage, source.sha
    if source is not None and source.release is not None:
        ctx.release = release.read_manifest(source.release)
        ctx.subject = str(ctx.release["subject"])
    # Known before any step runs: only a first install is ever taken back.
    ctx.first = not host.exists(layout.install_record(places.state))
    return ctx


def sentence(text: str) -> str:
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", ":", "?")) or "\n" in text else text + "."


def plain(ctx: Install, exc: BaseException) -> str:
    """What stopped the install, in one plain sentence that says what to do; the log keeps the detail."""
    if isinstance(exc, Refused):
        return sentence(exc.reason)
    doing = ctx.doing[:1].lower() + ctx.doing[1:] if ctx.doing else "starting"
    if isinstance(exc, KeyboardInterrupt):
        return "The install was stopped before it finished. Run it again to finish the job."
    if isinstance(exc, CommandError):
        return (f"The install could not finish {doing}: a command it ran failed. Run it again; if it stops here "
                "again, the log says why.")  # fmt: skip
    return (f"The install broke while {doing} ({type(exc).__name__}). Run it again; if it breaks here again, the "
            "log says why.")  # fmt: skip


def perform(ctx: Install, *, keep: bool) -> str | None:
    """Every step, as the command line and the console both run them. A failure is told through the host, then a
    failed first install is taken back; returns what stopped it, in plain words, or None once the install finished."""
    try:
        run(ctx)
    # telemetry: surfaced — what stopped the install is the `problem` the owner reads; the log has the trace
    except (Exception, KeyboardInterrupt) as exc:
        problem = plain(ctx, exc)
        ctx.host.say(f"stopped at step {ctx.reached} ({ctx.doing}): {type(exc).__name__}: {exc}")
        if not isinstance(exc, Refused | CommandError | KeyboardInterrupt):
            ctx.host.say("".join(traceback.format_exception(exc)))
        ctx.host.fail(problem)
        ctx.cancel_pending()
        if rolls_back(ctx, keep=keep):
            roll_back(ctx)
        return problem
    return None


ROLL_BACK = "Taking back the failed install"


def rolls_back(ctx: Install, *, keep: bool) -> bool:
    """Only a real first install that got as far as changing the machine, unless the owner keeps it to look at."""
    return not ctx.host.dry and ctx.first and ctx.reached >= FIRST_CHANGE and not keep


def roll_back(ctx: Install) -> None:
    """A first install that failed takes back everything it made, so the next one starts from a clean machine. A
    failed reinstall never does: removing it would delete the agents' clone, their queue and every stored secret."""
    from runtime.system import uninstall

    ctx.host.phase(len(PHASES) + 1, ROLL_BACK)
    try:
        uninstall.remove_all(ctx.host, ctx.places, ctx.owner.name, ctx.product, phases=False)
    # telemetry: surfaced — the owner is told through `fail` to uninstall, then install again
    except (Exception, KeyboardInterrupt) as exc:
        ctx.host.say(f"the roll-back stopped: {type(exc).__name__}: {exc}")
        ctx.host.fail("The install could not take back everything it made: uninstall the factory, then install again.")
        return
    ctx.host.done()
    ctx.host.tell("Everything this install made is removed again, so the computer is as it was before it.")


def parse_changes(items: list[str]) -> dict[str, Any]:
    """--set key=value: a TOML value (a number, a list, a quoted string), or else the text as written."""
    changes: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise refuse(f"--set {item}: write it as key=value")
        try:
            changes[key.strip()] = tomllib.loads(f"value = {raw}")["value"]
        # telemetry: expected — a value that is not TOML is kept as the text it is, as --set says
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
    sealed.exec_staged(sha, stage, "runtime/system/install.py", argv, checkout=REPO_ROOT)


def upgrade(commit: str) -> int:
    """The dashboard's upgrade, run by root's upgrade service from the release's sealed copy. Whose laptop it is and
    which product checkout come from root's own record of the last install; a step that would ask stops it."""
    places = Places.system()
    if os.geteuid() != 0 or environment.staged() is None:
        print(f"{PROGRAM}: --upgrade runs as root, from a release's sealed copy", file=sys.stderr)
        return 2
    try:
        installed = upgrades.read_installed(places)
    except store.StoreError as exc:
        print(f"{PROGRAM}: the record of the last install cannot be read ({exc}): upgrade by hand", file=sys.stderr)
        return 2
    owner = RealHost().account(installed["owner"]) if installed else None
    if installed is None or owner is None:
        print(f"{PROGRAM}: there is no install to upgrade, or its owner's account is gone", file=sys.stderr)
        return 2
    journal = Journal.create(layout.LOGS, "upgrade", group=owner.groups[0], verbose=False, terminal=False)
    host = UnattendedHost(journal, places, installed["commit"], commit)
    ctx = job(host, places, owner, Path(installed["product"]), commit, {}, False)
    ctx.unattended = True
    problem: str | None = "The upgrade was stopped before it finished. Upgrade again to finish the job."
    try:
        if ctx.first:
            raise refuse("There is no install to upgrade: install the factory by hand first")
        problem = perform(ctx, keep=False)
        if problem is None:
            host.progress(upgrades.DONE, "")
    finally:
        sealed.finish(RealHost(journal), ctx.stage, kept=False)
    return 0 if problem is None else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True, help="the factory commit to install")
    parser.add_argument("--repo", type=Path, help="your checkout of the product the factory works on")
    parser.add_argument("--upgrade", action="store_true",
                        help="the dashboard's upgrade, as root's upgrade service runs it: it asks nothing")  # fmt: skip
    parser.add_argument("--healthchecks", action="store_true", help="create the Healthchecks.io checks")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="a setting to save")
    parser.add_argument("--dry-run", action="store_true", help="go through every step; change nothing")
    keep = "leave a failed first install in place, to look at it, instead of removing it"
    parser.add_argument("--keep-on-failure", action="store_true", help=keep)
    parser.add_argument("--verbose", action="store_true", help="print every detail the log keeps")
    args = parser.parse_args(argv)
    if args.upgrade:
        return upgrade(args.commit)
    if args.repo is None:
        parser.error("--repo is required: your checkout of the product")
    # sudo names the owner who ran it; a dry run without sudo has none, and is then the owner's own.
    sudo_user = environment.sudo_user()
    root = os.geteuid() == 0 and sudo_user is not None
    if not args.dry_run and not root:
        print("Run it with sudo, as yourself (or add --dry-run).", file=sys.stderr)
        return 2
    owner = RealHost().account(getpass.getuser() if sudo_user is None else sudo_user)
    if owner is None:
        print(f"{PROGRAM}: the owner's account cannot be read", file=sys.stderr)
        return 2
    if not args.dry_run and environment.staged() is None:
        return from_commit(args.commit, owner.name, argv)
    if root:
        journal = Journal.create(layout.LOGS, "install", group=owner.groups[0], verbose=args.verbose)
    else:
        journal = Journal(verbose=True)  # a dry run as the owner keeps no log: what it would do is its whole point
    host: Host = LookingHost(journal) if args.dry_run else RealHost(journal)
    ctx = job(host, Places.system(), owner, args.repo, args.commit, parse_changes(args.set), args.healthchecks)
    verb = "Upgrading" if not ctx.first else "Installing"
    journal.screen(f"{'Dry run: ' if args.dry_run else ''}{verb} the factory at {(ctx.sha or ctx.commit)[:12]}"
                   f"{f': {ctx.subject}' if ctx.subject else ''}")  # fmt: skip
    problem: str | None = "interrupted"
    try:
        problem = perform(ctx, keep=args.keep_on_failure)
    finally:
        # A real host even for a dry run: the copy a dry run ran from was made for real.
        sealed.finish(RealHost(journal), ctx.stage, kept=problem is not None and args.keep_on_failure)
    if problem is None:
        found = steps_wire.dashboard_address(host)
        journal.screen("The dry run finished. Nothing changed." if args.dry_run else
                       f"The factory is installed.{f' Open the dashboard: {found}' if found else ''}")  # fmt: skip
    if journal.path:
        journal.screen(journal.where())
    return 0 if problem is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
