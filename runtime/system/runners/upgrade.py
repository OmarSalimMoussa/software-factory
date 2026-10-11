"""software-upgrade@<commit>.service: the owner's Upgrade from the dashboard, as root, to the release of <commit>.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/upgrade.py <commit>``. The warden may start this unit
and nothing more, and the runner trusts no word of it: whose laptop, which product, which factory repo and the token
come from root's own records of the last install. On GitHub it checks the release: CI's bot made it of that commit
and uploaded its files, its tag points at the commit, CI passed on a push of it to the factory's default branch, the
commit is on that branch, and it moves forward from the installed commit. It downloads the release file, checks it
against the checksum beside it and its own manifest, and runs it with --upgrade, which asks nothing and writes each
step where the dashboard reads it. Stdlib only.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment, releases, store, upgrades  # noqa: E402 - the path line above must run first
from runtime.shared import settings as settings_module  # noqa: E402
from runtime.shared.paths import Places  # noqa: E402
from runtime.system import layout, release  # noqa: E402

CHECKING, DOWNLOADING, UPGRADING = "Checking the release", "Downloading the release", "Upgrading"


class Stop(Exception):
    """Why the upgrade stops, in the owner's words."""


def check(rel: releases.Releases, installed: str, commit: str) -> releases.Release:
    found = rel.of_commit(commit)
    branch = rel.default_branch()
    if not rel.ci_passed(commit, branch):
        raise Stop(f"The factory's CI has not passed on {commit[:12]} pushed to {branch}, so it is not installed.")
    newer, _ = rel.changes(installed, commit)
    if newer != "ahead":
        raise Stop(f"The release {commit[:12]} is not newer than the installed factory ({installed[:12]}).")
    on, _ = rel.changes(commit, branch)
    if on not in ("ahead", "identical"):
        raise Stop(f"The release {commit[:12]} is not on the factory's {branch}, so it is not installed.")
    if releases.ASSET not in found.assets or releases.CHECKSUM not in found.assets:
        raise Stop(f"The release {found.tag} has no {releases.ASSET} with its checksum.")
    return found


def fetch(rel: releases.Releases, found: releases.Release, commit: str, folder: Path) -> Path:
    data = rel.download(found.assets[releases.ASSET])
    listed = rel.download(found.assets[releases.CHECKSUM]).decode(errors="replace").split()
    if not listed or hashlib.sha256(data).hexdigest() != listed[0]:
        raise Stop("The downloaded release file does not match its checksum. Upgrade again.")
    try:
        archive = release.Archive(data)
    except release.ReleaseError as exc:
        raise Stop(f"The downloaded release file cannot be read ({exc}). Upgrade again.") from exc
    if archive.problems() or archive.sha != commit:
        raise Stop(f"The downloaded release file is not the release of {commit[:12]}. Upgrade again.")
    path = folder / releases.ASSET
    path.write_bytes(data)
    path.chmod(0o700)
    return path


def http_timeout(places: Places) -> float:
    """The release's own default, read alone: the overrides are the warden's file, which root does not read here, and
    the defaults leave the install's own values (repo.path) empty."""
    tree = tomllib.loads(places.defaults.read_text(encoding="utf-8"))
    return float(settings_module.read_some(tree, ("watch.http_timeout_seconds",))["watch.http_timeout_seconds"])


def upgrade(places: Places, commit: str) -> int:
    installed = upgrades.read_installed(places)
    if installed is None:
        raise Stop("Nothing is installed to upgrade. Install the factory by hand first.")

    def report(step: str) -> None:
        upgrades.write_progress(places, installed["commit"], commit, upgrades.RUNNING, step)

    folder = Path(tempfile.mkdtemp(prefix="download-", dir=places.upgrade))
    try:
        report(CHECKING)
        timeout = http_timeout(places)
        rel = releases.Releases.of(upgrades.token_path(places), installed["repo"], timeout, releases.http_get)
        found = check(rel, installed["commit"], commit)
        report(DOWNLOADING)
        path = fetch(rel, found, commit, folder)
        report(UPGRADING)
        env = {"PATH": layout.ROOT_PATH, "LANG": "C.UTF-8", "HOME": "/root"}
        done = subprocess.run([layout.PYTHON, "-I", str(path), "--upgrade"], env=env, check=False)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    said = upgrades.read_progress(places)
    if done.returncode != 0 and (said is None or said["state"] != upgrades.FAILED):
        raise Stop(f"The upgrade stopped (exit {done.returncode}). Its log in {layout.LOGS} says why.")
    return done.returncode


def ended(places: Places, commit: str) -> int:
    """The unit's last word, however it ended (a timeout's kill included): an upgrade still running stopped."""
    said = upgrades.read_progress(places)
    if said is not None and said["to"] == commit and said["state"] == upgrades.RUNNING:
        result = environment.systemd_result()
        why = f" ({result})" if result is not None else ""
        problem = f"The upgrade stopped before it finished{why}. Its log in {layout.LOGS} says more; upgrade again."
        upgrades.write_progress(places, said["from"], commit, upgrades.FAILED, "", problem, said["log"])
    return 0


def main(argv: list[str]) -> int:
    finished = argv[:1] == ["--ended"]
    given = argv[1:] if finished else argv
    if len(given) != 1 or not releases.FULL_SHA.match(given[0]):
        print("usage: upgrade.py [--ended] <the full commit of the release to install>", file=sys.stderr)
        return 2
    places, commit = Places.system(), given[0]
    if finished:
        return ended(places, commit)
    try:
        return upgrade(places, commit)
    except Exception as exc:
        said = str(exc) if isinstance(exc, Stop) else f"The upgrade stopped: {exc}. Upgrade again, or by hand."
        print(f"upgrade: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if not isinstance(exc, Stop | releases.ReleaseError):
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
