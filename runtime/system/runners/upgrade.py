"""software-upgrade@<commit>.service: an upgrade of the factory to <commit>, as root. Installing the commit before is
the way back.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/upgrade.py <commit>``. The warden may start this unit
and nothing more, and the runner takes no other word of it: whose machine, which product, which factory repo and the
token come from root's own records of the last install. It downloads GitHub's tarball of the commit, unpacks it into a
new sealed copy, and runs that copy's install with --upgrade, which asks nothing and writes each step where the
dashboard reads it. Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tomllib
import traceback
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment, store, upgrades
from runtime.shared import settings as settings_module
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import factory_repo, layout, sealed

DOWNLOADING, UPGRADING = "Downloading the factory", "Upgrading"
INSTALL = "runtime/system/install.py"


class Stop(Exception):
    """Why the upgrade stops, in the owner's words."""


def http_timeout(places: Places) -> float:
    """The installed factory's own default, read alone: the overrides are the warden's file, which root does not read
    here, and the defaults leave the install's own values (repo.path) empty."""
    tree = tomllib.loads(places.defaults.read_text(encoding="utf-8"))
    return float(settings_module.read_some(tree, ("watch.http_timeout_seconds",))["watch.http_timeout_seconds"])


def upgrade(places: Places, commit: str, get: factory_repo.Get, stages: Path) -> int:
    installed = upgrades.read_installed(places)
    if installed is None:
        raise Stop("Nothing is installed to upgrade. Install the factory by hand first.")

    def report(step: str) -> None:
        upgrades.write_progress(places, installed["commit"], commit, upgrades.RUNNING, step)

    report(DOWNLOADING)
    token = factory_repo.read_token(upgrades.token_path(places))
    data = factory_repo.FactoryRepo(installed["repo"], token, http_timeout(places), get).tarball(commit)
    try:
        stage = sealed.unpack(data, commit, stages)
    except tarfile.TarError as exc:
        raise Stop(f"The factory GitHub sent cannot be unpacked ({type(exc).__name__}). Upgrade again.") from exc
    except Refused as exc:
        raise Stop(f"{exc.reason}. Upgrade again.") from exc
    try:
        report(UPGRADING)
        code = sealed.run_staged(commit, stage, INSTALL, ["--commit", commit, "--upgrade"])
    finally:
        sealed.discard(stage)
    said = upgrades.read_progress(places)
    if code != 0 and (said is None or said["state"] != upgrades.FAILED):
        raise Stop(f"The upgrade stopped (exit {code}). Its log in {layout.LOGS} says why.")
    return code


def ended(places: Places, commit: str) -> int:
    """The unit's last word, however it ended (a timeout's kill included): an upgrade still running stopped, and the
    factory's services run again, so the dashboard says how it ended. The factory stays paused."""
    said = upgrades.read_progress(places)
    if said is not None and said["to"] == commit and said["state"] == upgrades.RUNNING:
        result = environment.systemd_result()
        why = f" ({result})" if result is not None else ""
        problem = f"The upgrade stopped before it finished{why}. Its log in {layout.LOGS} says more; upgrade again."
        upgrades.write_progress(places, said["from"], commit, upgrades.FAILED, "", problem, said["log"])
    started = subprocess.run(["systemctl", "start", "--no-block", *layout.SERVICES], check=False)
    if started.returncode != 0:
        print(f"upgrade: the factory's services did not start again (exit {started.returncode})", file=sys.stderr)
    return started.returncode


def main(argv: list[str]) -> int:
    finished = argv[:1] == ["--ended"]
    given = argv[1:] if finished else argv
    if len(given) != 1 or not upgrades.FULL_SHA.match(given[0]):
        print("usage: upgrade.py [--ended] <the full commit of the factory to install>", file=sys.stderr)
        return 2
    places, commit = Places.system(), given[0]
    if finished:
        return ended(places, commit)
    try:
        return upgrade(places, commit, factory_repo.http_get, layout.STAGE_ROOT)
    except Exception as exc:  # noqa: BLE001 - whatever stops an upgrade is recorded where the dashboard shows it
        said = str(exc) if isinstance(exc, Stop) else f"The upgrade stopped: {exc}. Upgrade again, or by hand."
        print(f"upgrade: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if not isinstance(exc, Stop | factory_repo.RepoError):
            traceback.print_exc()
        try:
            frm = (upgrades.read_installed(places) or {}).get("commit", "")
        except store.StoreError as record:
            print(f"upgrade: the install record cannot be read ({record})", file=sys.stderr, flush=True)
            frm = ""
        upgrades.write_progress(places, frm, commit, upgrades.FAILED, "", said)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
