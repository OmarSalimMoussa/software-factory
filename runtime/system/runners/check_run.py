"""software-check-run@<id>.service: one `factory check` as the agent user, walled into its checkout.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/check_run.py <state> <id>``. The checks run the
product's own code (``scripts/dev deps`` and ``test``), so the runner starts them under bubblewrap: the agent's home
is hidden but for the checkout, its tools' caches and, read-only, the main checkout and the Claude settings it
tracks; every other worktree and the agent's Claude folder are gone; the git folder the merge hook and every
worktree share is read-only, so nothing the check runs can plant a hook or forge what the warden records. Its exit
status is the check's, which systemd hands to the warden. Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment
from runtime.shared.paths import CHECK_TOOLS
from runtime.system import layout
from runtime.system.runners import launch_file as lf
from runtime.warden.sessions import session_settings

INSTALL = Path(__file__).resolve().parents[3]
# Under the tools' Python in isolated mode: the install's root comes in as the first argument.
RUN_CHECKS = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); from checks.runner.cli import main; sys.exit(main(sys.argv[1:]))"
)


def git_folders(checkout: Path, timeout: int) -> tuple[Path, Path]:
    """The checkout's main checkout and the git folder every worktree of it shares, as git names them. An fsmonitor
    the repo's config names never runs: git reads that setting from a file product code could have written."""
    done = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=checkout, capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip
    common = Path(done.stdout.strip())
    if done.returncode != 0 or common.name != ".git" or not common.is_dir():
        raise lf.BadRequest(f"{checkout} is not a git checkout: {done.stderr.strip()[-200:]}")
    return common.parent, common


def walls(checkout: Path, main: Path, common: Path, home: Path, caches: list[Path]) -> list[str]:
    """bubblewrap's mounts, in the order it makes them: a later one lies over an earlier one."""
    argv = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(home)]
    argv += ["--bind" if main == checkout else "--ro-bind", str(main), str(main)]
    argv += _claude(main)
    worktrees = session_settings.worktrees(main)
    worktrees.mkdir(exist_ok=True)  # made now, so a worktree made during the check is hidden too
    argv += ["--tmpfs", str(worktrees)]
    if main != checkout:
        argv += ["--bind", str(checkout), str(checkout), *_claude(checkout)]
    argv += ["--ro-bind", str(common), str(common)]
    for cache in caches:
        if cache.is_dir():
            argv += ["--bind", str(cache), str(cache)]
    return argv


def _claude(root: Path) -> list[str]:
    """``root``'s Claude folder, made now, read-only (git finds what it tracks; no check writes it), never a link."""
    folder = root / ".claude"
    if folder.is_symlink():
        raise lf.BadRequest(f"{folder} is a link: a check walls in only a folder of its own")
    folder.mkdir(exist_ok=True)
    return ["--ro-bind", str(folder), str(folder)]


def command(checkout: Path, args: list[str], home: Path, caches: list[Path], timeout: int) -> list[str]:
    main, common = git_folders(checkout, timeout)
    python = CHECK_TOOLS / "bin" / "python"
    return [
        str(layout.BWRAP), "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        *walls(checkout, main, common, home, caches),
        "--setenv", "HOME", str(home), "--setenv", "GIT_OPTIONAL_LOCKS", "0", "--chdir", str(checkout),
        "--", str(python), "-I", "-c", RUN_CHECKS, str(INSTALL), *args,
    ]  # fmt: skip


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    home = Path.home()
    try:
        raw = lf.read(state, request_id, (lf.CHECK,))
        settings, _ = lf.published(state)
        checkout, args = lf.check_parts(raw, home)
        timeout = settings.get_int("checks.run_minutes") * 60
        caches = [home / rel for rel in settings.get_list("sandbox.writable_caches")]
        argv_ = command(checkout, args, home, caches, settings.get_int("factory.command_seconds"))
    # telemetry: surfaced — the refusal is `print`ed to the unit's log, and the exit says it was refused
    except lf.BadRequest as exc:
        print(f"check-run: refused: {exc}", flush=True)
        return lf.REFUSED
    # telemetry: surfaced — the timeout is `print`ed to the unit's log, and the exit says so
    except subprocess.TimeoutExpired:
        print("check-run: git did not answer in time", flush=True)
        return lf.TIMED_OUT
    return lf.run(argv_, checkout, environment.handed_on(), timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
