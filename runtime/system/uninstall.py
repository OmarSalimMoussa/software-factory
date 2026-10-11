"""Remove everything the installer made, as root, so the next install starts from a clean machine:

    sudo python3 runtime/system/uninstall.py [--dry-run]

It refuses while any session is working or waiting. It stops and removes the factory's units, rules, firewall table,
AppArmor profile, managed settings, commands and programs; the warden's state (its secrets and the 6-digit code's
secret included) and the laptop's shared services with their data; the four factory users with their homes (the
agents' clone and the stack user's Docker data); and the factory's git hooks setting in the owner's checkout. It never
removes packages, the owner's own files, or anything on the server or on GitHub: it prints what to remove there.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared import paths, store  # noqa: E402 - the path line above must run first
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, MANAGED_SETTINGS, SLICE, WARDEN_USER, Places  # noqa: E402
from runtime.shared.protocol import Refused  # noqa: E402
from runtime.system import layout, steps_prepare  # noqa: E402
from runtime.system.host import Account, CommandError, DryHost, Host, RealHost  # noqa: E402
from runtime.system.job import REPO_ROOT, Install  # noqa: E402

UNITS = (layout.WARDEN_UNIT, layout.DASHBOARD_UNIT, layout.DAEMON_UNIT, layout.FIREWALL_UNIT, layout.SERVICES_UNIT)
TEMPLATES = (layout.AGENT_RUN, layout.STACK_RUN)
GUARD = "/opt/software-factory/runtime/guard/hook.py"  # the installed guard, named in the factory's managed settings
HOOKS = str(paths.INSTALL / "checks" / "git-hooks")


class LookingHost(DryHost):
    """A dry run that looks at this machine: it reads for real and changes nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.real = RealHost()

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
        return self.real.query(argv, user=user, cwd=cwd)


def _try(host: Host, argv: list[str]) -> None:
    """A change whose failure is no reason to stop: a unit already gone, a user with no processes."""
    if host.dry:
        host.say(f"  would run: {' '.join(argv)}")
        return
    code, out = host.query(argv)
    host.say(f"  ran: {' '.join(argv)}" + ("" if code == 0 else f" (exit {code}: {out.strip()[-160:]})"))


def stop(host: Host) -> None:
    host.say("1. Stop the factory")
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


def system_files(host: Host) -> None:
    host.say("2. Remove the units, rules, firewall, AppArmor profile and managed settings")
    for name in (*UNITS, *TEMPLATES, f"{SLICE}"):
        _remove(host, layout.UNITS_DIR / name)
    _remove(host, layout.SLICE_CONTROL / f"{SLICE}.d")
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
            host.say(f"  kept {MANAGED_SETTINGS}: it is not the factory's")


def programs(host: Host, dashboard_port: str) -> None:
    host.say("3. Remove the factory's commands and programs")
    for path in (paths.FACTORY_BIN, layout.CLAUDE_BIN.parent, paths.INSTALL, layout.TOOLS, layout.TOOLCHAIN,
                 paths.RUN, layout.DRILLS):  # fmt: skip
        _remove(host, path)
    code, served = host.query(["tailscale", "serve", "status"])
    if code == 0 and f"127.0.0.1:{dashboard_port}" in served:
        _try(host, ["tailscale", "serve", "--https=443", "off"])


def state(host: Host, places: Places) -> None:
    host.say("4. Remove the warden's state and the laptop's shared services")
    _remove(host, places.state)
    _remove(host, paths.SERVICES)


def users(host: Host) -> None:
    host.say("5. Remove the factory's users, their homes and groups")
    for user in (AGENT_USER, layout.STACK_USER):
        if host.account(user) is not None:
            host.run(["userdel", "--remove", user])
        _remove(host, Path("/home") / user)
    for user in (WARDEN_USER, DASHBOARD_USER):
        if host.account(user) is not None:
            host.run(["userdel", user])
    for group in (layout.SLOTS_GROUP, *layout.USERS):
        code, _ = host.query(["getent", "group", group])
        if code == 0:
            host.run(["groupdel", group])


def owner_checkout(host: Host, owner: str, repo: Path | None) -> None:
    host.say("6. Take the factory's git hooks off your checkout")
    if repo is None:
        host.say("  no --repo given: run `git config --unset core.hooksPath` in your checkout if it names the factory")
        return
    code, current = host.query(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], user=owner)
    if code == 0 and current.strip() == HOOKS:
        host.run(["git", "-C", str(repo), "config", "--unset", "core.hooksPath"], user=owner)


def _remove(host: Host, path: Path) -> None:
    if host.exists(path):
        host.remove(path)
        if not host.dry:
            host.say(f"  removed {path}")


def remove_all(host: Host, places: Places, owner: str, repo: Path | None) -> None:
    """Every removal, in order; the installer calls it too, to take back a first install that failed."""
    port = _dashboard_port(places)
    stop(host)
    system_files(host)
    programs(host, port)
    state(host, places)
    users(host)
    owner_checkout(host, owner, repo)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="uninstall.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, help="your checkout of the product, to take the factory's hooks off it")
    parser.add_argument("--dry-run", action="store_true", help="print every step; change nothing")
    args = parser.parse_args(argv)
    if not args.dry_run and (os.geteuid() != 0 or "SUDO_USER" not in os.environ):
        print("uninstall.py: run it with sudo, as the owner (or add --dry-run)", file=sys.stderr)
        return 2
    host: Host = LookingHost() if args.dry_run else RealHost()
    owner = RealHost().account(os.environ.get("SUDO_USER") or getpass.getuser())
    if owner is None:
        print("uninstall.py: the owner's account cannot be read", file=sys.stderr)
        return 2
    places = Places.system()
    ctx = Install(host, places, REPO_ROOT, (args.repo or Path(".")).resolve(), "HEAD", owner, {}, False)
    try:
        try:
            steps_prepare.refuse_live_sessions(ctx)
        except store.StoreError:
            if not host.dry:
                raise
            host.say("  (a dry run cannot read the sessions' registry; the uninstall checks it as root)")
        remove_all(host, places, owner.name, args.repo.resolve() if args.repo else None)
    except Refused as exc:
        print(f"uninstall.py: refused: {exc.reason}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(f"uninstall.py: {exc}", file=sys.stderr)
        return 1
    host.say(
        "The factory is removed from this machine. Left for you: on GitHub, the warden's deploy key on the product "
        "(titled 'software-factory deploy' or as you named it), which the next install replaces; the server is "
        "untouched."
    )
    return 0


def _dashboard_port(places: Places) -> str:
    """The port the dashboard was published on: the warden's published settings, or else this commit's default."""
    published = places.state / "public" / "settings.json"
    try:
        return str(json.loads(published.read_text(encoding="utf-8"))["dashboard"]["port"])
    # telemetry: expected — no install, or one that never published its settings: the commit's own default stands
    except (OSError, ValueError, KeyError, TypeError):
        return str(tomllib.loads((REPO_ROOT / "settings.toml").read_text(encoding="utf-8"))["dashboard"]["port"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
