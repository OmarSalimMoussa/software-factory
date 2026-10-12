"""Remove everything the installer made, as root, so the next install starts from a clean machine. The release file
runs it (``sudo python3 software-factory.pyz --uninstall``); from the owner's clone of the factory it is

    sudo python3 runtime/system/uninstall.py [--repo <product checkout>] [--dry-run] [--verbose]

It refuses while any session is working or waiting. It stops and removes the factory's units, rules, firewall table,
AppArmor profile, managed settings, commands and programs, and the sealed copies installs ran from; the warden's state
(its secrets and the 6-digit code's secret included) and the laptop's shared services with their data; the four
factory users with their homes (the agents' clone and the stack user's Docker data); and the factory's git hooks
setting in the owner's checkout. It never removes packages, the owner's own files, the install logs, or anything on
the server or on GitHub: it says what to remove there. Like the installer, it shows a few phases and logs the rest.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path

# Run by its path, which puts no folder of the factory's on sys.path: its root goes first, for the imports below.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared import environment, paths, store
from runtime.shared import settings as settings_module
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, MANAGED_SETTINGS, SLICE, WARDEN_USER, Places
from runtime.shared.protocol import Refused
from runtime.system import layout, sealed
from runtime.system.github_keys import WARDEN_TITLE
from runtime.system.host import Account, CommandError, DryHost, Host
from runtime.system.install_steps import steps_prepare, steps_wire
from runtime.system.job import REPO_ROOT, Install
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost

PROGRAM = "uninstall.py"

UNITS = (
    layout.WARDEN_UNIT,
    layout.DASHBOARD_UNIT,
    layout.DAEMON_UNIT,
    layout.FIREWALL_TIMER,
    layout.FIREWALL_REFRESH,
    layout.FIREWALL_UNIT,
    layout.SERVICES_UNIT,
)
TEMPLATES = (layout.AGENT_RUN, layout.STACK_RUN, layout.CHECK_RUN, layout.UPGRADE_RUN)
GUARD = "/opt/software-factory/runtime/guard/hook.py"  # the installed guard, named in the factory's managed settings
HOOKS = str(paths.INSTALL / "checks" / "git-hooks")


class LookingHost(DryHost):
    """A dry run that looks at this machine: it reads for real and changes nothing."""

    def __init__(self, journal: Journal | None = None) -> None:
        super().__init__(journal=journal or Journal())
        self.real = RealHost(self.journal)

    def exists(self, path: Path) -> bool:
        try:
            return self.real.exists(path)
        # telemetry: expected — a dry run is not root: what it may not look at is listed as if present
        except PermissionError:
            return True

    def read(self, path: Path) -> str:
        return self.real.read(path)

    def account(self, name: str) -> Account | None:
        return self.real.account(name)

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        # Started without sudo, a dry run is the owner's own process: a command as the owner runs as it is.
        mine = bool(user) and os.geteuid() != 0 and user == getpass.getuser()
        return self.real.query(argv, user="" if mine else user, cwd=cwd)


def _try(host: Host, argv: list[str]) -> None:
    """A change whose failure is no reason to stop: a unit already gone, a user with no processes."""
    if host.dry:
        host.say(f"would run: {' '.join(argv)}")
        return
    host.query(argv)


def stop(host: Host) -> None:
    instances = [f"{unit.removesuffix('.service')}*" for unit in TEMPLATES]
    _try(host, ["systemctl", "stop", *UNITS, *instances])
    _try(host, ["systemctl", "disable", *UNITS])
    for user in (layout.STACK_USER, AGENT_USER):
        account = host.account(user)
        if account is None:
            continue
        _try(host, ["loginctl", "disable-linger", user])
        _try(host, ["systemctl", "stop", f"user@{account.uid}.service"])
        _try(host, ["pkill", "-KILL", "-u", user])
    # The slice and the parent systemd makes from its name stay active, empty, until stopped or the next boot.
    _try(host, ["systemctl", "stop", SLICE, f"{SLICE.split('-')[0]}.slice"])


def system_files(host: Host) -> None:
    for name in (*UNITS, *TEMPLATES, f"{SLICE}"):
        _remove(host, layout.UNITS_DIR / name)
    _remove(host, layout.SLICE_CONTROL / f"{SLICE}.d")
    stack = host.account(layout.STACK_USER)
    if stack is not None:
        _remove(host, layout.stack_wall(str(stack.uid)).parent)
    _remove(host, layout.SLEEP_HOOK)
    _try(host, ["systemctl", "daemon-reload"])
    _try(host, ["systemctl", "reset-failed"])
    _remove(host, layout.POLKIT_RULE)
    _try(host, ["nft", "delete", "table", "inet", layout.NFT_TABLE])
    _remove(host, layout.CONFIG)
    if host.exists(layout.APPARMOR_PROFILE):
        _try(host, ["apparmor_parser", "-R", str(layout.APPARMOR_PROFILE)])
        _remove(host, layout.APPARMOR_PROFILE)
    # Only the factory's own: a machine's managed settings may be someone else's, and stay then.
    if host.exists(MANAGED_SETTINGS):
        if GUARD in host.read(MANAGED_SETTINGS):
            _remove(host, MANAGED_SETTINGS)
        else:
            host.say(f"kept {MANAGED_SETTINGS}: it is not the factory's")


def _with_leftovers(path: Path) -> tuple[Path, ...]:
    """``path`` and what a stopped install may have left half made beside it."""
    return (path, *(path.with_name(f"{path.name}.{end}") for end in ("new", "old", "partial", "bundle")))


def programs(host: Host, places: Places, dashboard_port: str | None) -> None:
    for path in (paths.FACTORY_BIN, layout.CLAUDE_BIN.parent, *_with_leftovers(paths.INSTALL), layout.TOOLS,
                 *_with_leftovers(layout.TOOLCHAIN), paths.RUN, paths.FIREWALL, paths.UPGRADE,
                 layout.DRILLS):  # fmt: skip
        _remove(host, path)
    sealed_copies(host)
    tailnet(host, places, dashboard_port)


def tailnet(host: Host, places: Places, dashboard_port: str | None) -> None:
    """Take the dashboard off the tailnet, but never another handler on its port, and put back the operator the
    install cleared."""
    record = steps_wire.read_tailnet_record(host, places.state)
    served = record.get("served") if isinstance(record.get("served"), str) else None
    ours = f"http://127.0.0.1:{dashboard_port}" if dashboard_port is not None else served
    code, status = host.query(["tailscale", "serve", "status", "--json"])
    handlers = steps_wire.https_handlers(status) if code == 0 else None
    if ours is None:
        host.tell("The dashboard's port could not be read, so nothing is taken off your tailnet: check "
                  "tailscale serve status and remove the dashboard's entry yourself.")  # fmt: skip
    elif handlers and set(handlers.values()) == {ours}:
        _try(host, ["tailscale", "serve", "--https=443", "off"])
    elif handlers and ours in handlers.values():
        host.tell("Your tailnet's port 443 serves more than the dashboard, so it stays: take the dashboard's handler "
                  "off it yourself (tailscale serve status shows it).")  # fmt: skip
    operator = record.get("operator")
    if isinstance(operator, str) and operator:
        _try(host, ["tailscale", "set", f"--operator={operator}"])
    elif record and operator is None:
        host.tell("The install could not read your Tailscale operator, so it is not put back: if you had one, set it "
                  "again with sudo tailscale set --operator=<you>.")  # fmt: skip


def sealed_copies(host: Host) -> None:
    """Every sealed copy but the one this runs from (the console, or a first install taking itself back), which goes
    when its program ends."""
    if not REPO_ROOT.is_relative_to(layout.STAGE_ROOT):
        _remove(host, layout.STAGE_ROOT)
        return
    own = layout.STAGE_ROOT / REPO_ROOT.relative_to(layout.STAGE_ROOT).parts[0]
    _, listed = host.query(["find", str(layout.STAGE_ROOT), "-mindepth", "1", "-maxdepth", "1"])
    for line in listed.splitlines():
        if line.strip() and Path(line.strip()) not in (own, sealed.release_folder(own)):
            _remove(host, Path(line.strip()))
    host.say(f"kept {own}: this runs from it, and it goes when it ends")


def state(host: Host, places: Places) -> None:
    _remove(host, places.state)
    _remove(host, paths.SERVICES)
    _remove(host, paths.ERRORS)


def users(host: Host) -> None:
    for user in (AGENT_USER, layout.STACK_USER):
        if host.account(user) is not None:
            host.run(["userdel", "--remove", user])
        for home in _with_leftovers(Path("/home") / user):
            _remove(host, home)
    for user in (WARDEN_USER, DASHBOARD_USER):
        if host.account(user) is not None:
            host.run(["userdel", user])
    for group in (layout.SLOTS_GROUP, *layout.USERS):
        code, _ = host.query(["getent", "group", group])
        if code == 0:
            host.run(["groupdel", group])


def owner_checkout(host: Host, owner: str, repo: Path | None) -> None:
    if repo is None:
        host.tell("Your product checkout was not named, so it may still run the factory's git hooks: run git config "
                  "--unset core.hooksPath in it.")  # fmt: skip
        return
    code, current = host.query(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], user=owner)
    if code == 0 and current.strip() == HOOKS:
        host.run(["git", "-C", str(repo), "config", "--unset", "core.hooksPath"], user=owner)


def _remove(host: Host, path: Path) -> None:
    if host.exists(path):
        host.remove(path)


STEPS = (
    "Stopping the factory",
    "Removing its services, rules and firewall",
    "Removing its programs",
    "Removing its data and the shared services",
    "Removing its users",
    "Taking its git hooks off your checkout",
)
LEFT_FOR_YOU = (
    f"Left for you: on GitHub, the warden's deploy key on the product (titled '{WARDEN_TITLE}' or as you named it), "
    f"which the next install replaces. The server is untouched, and the install logs stay in {layout.LOGS}."
)


def remove_all(host: Host, places: Places, owner: str, repo: Path | None, *, phases: bool) -> None:
    """Every removal, in order, each its own phase or, when the installer takes back a first install that failed,
    each a step of its one phase."""
    port = dashboard_port(places)
    removals: tuple[Callable[[], None], ...] = (
        lambda: stop(host),
        lambda: system_files(host),
        lambda: programs(host, places, port),
        lambda: state(host, places),
        lambda: users(host),
        lambda: owner_checkout(host, owner, repo),
    )
    for number, (title, removal) in enumerate(zip(STEPS, removals, strict=True), 1):
        if phases:
            host.phase(number, title)
        host.step(number, title)
        removal()
    if phases:
        host.done()


def perform(host: Host, places: Places, owner: Account, repo: Path | None) -> str | None:
    """The uninstall, as the command line and the console both run it: refused while a session is working or
    waiting. A failure is told through the host; returns what stopped it, or None once everything is removed."""
    ctx = Install(host, places, REPO_ROOT, (repo or Path(".")).resolve(), "HEAD", owner, {}, False)
    try:
        try:
            steps_prepare.refuse_live_sessions(ctx)
        except store.StoreError:
            if not host.dry:
                raise
            host.say("(a dry run cannot read the sessions' registry; the uninstall checks it as root)")
        remove_all(host, places, owner.name, repo.resolve() if repo else None, phases=True)
    # telemetry: surfaced — the refusal is the `problem` the uninstall ends with
    except Refused as exc:
        problem = exc.reason[:1].upper() + exc.reason[1:]
    # telemetry: surfaced — the failed command is told, and the `problem` the uninstall ends with says what to do
    except CommandError as exc:
        host.say(str(exc))
        problem = ("The uninstall could not finish: a command it ran failed. Run it again; if it stops here again, the "
                   "log says why.")  # fmt: skip
    else:
        return None
    host.fail(problem)
    return problem


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, help="your checkout of the product, to take the factory's hooks off it")
    parser.add_argument("--dry-run", action="store_true", help="go through every step; change nothing")
    parser.add_argument("--verbose", action="store_true", help="print every detail the log keeps")
    args = parser.parse_args(argv)
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
    if root:
        journal = Journal.create(layout.LOGS, "uninstall", group=owner.groups[0], verbose=args.verbose)
    else:
        journal = Journal(verbose=True)  # a dry run as the owner keeps no log: what it would do is its whole point
    host: Host = LookingHost(journal) if args.dry_run else RealHost(journal)
    journal.screen("Dry run: what Uninstall removes" if args.dry_run else "Removing the factory from this computer")
    source = environment.staged()
    try:
        failed = perform(host, Places.system(), owner, args.repo) is not None
    finally:
        if source is not None:  # run from a release, from a sealed copy that goes now
            sealed.finish(RealHost(journal), source.stage, kept=False)
    if not failed:
        journal.screen("Nothing changed." if args.dry_run else "The factory is removed from this computer.")
        journal.screen(LEFT_FOR_YOU)
    if journal.path:
        journal.screen(journal.where())
    return 1 if failed else 0


def dashboard_port(places: Places) -> str | None:
    """The port the dashboard was published on, from the warden's published settings; None when they cannot be
    read (no install, or one that never published them)."""
    published = places.state / "public" / "settings.json"
    try:
        return str(settings_module.read_some(store.read_json(published), ("dashboard.port",))["dashboard.port"])
    # telemetry: surfaced — the uninstall says it could not tell the port `dashboard_port` returns as None
    except (OSError, store.StoreError, settings_module.SettingsError):
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
