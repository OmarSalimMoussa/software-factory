"""The sealed copy an install, an uninstall or the console runs from: one commit, from the owner's checkout or from a
release file, unpacked by root into a fresh root-owned folder and made read-only before anything runs from it.
Nothing in the checkout's working files, and nothing in the environment the owner started it from, can change what
runs: the copy starts again as `python3 -I` with an environment written out in full.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from runtime.system import layout
from runtime.system.host import Host
from runtime.system.job import refuse

# Set only in the environment of a process that runs from a sealed copy: the copy, its commit, and where it came from:
# the owner's checkout, or a release file whose manifest and history wait in the release folder beside the copy.
STAGE, SHA = "SOFTWARE_INSTALL_STAGE", "SOFTWARE_INSTALL_SHA"
CHECKOUT, RELEASE = "SOFTWARE_INSTALL_CHECKOUT", "SOFTWARE_INSTALL_RELEASE"
# What a sealed copy starts with, beside the variables above, the owner's name and their terminal's type.
BASE_ENV = {"PATH": layout.ROOT_PATH, "LANG": "C.UTF-8", "HOME": "/root"}
# The owner's desktop session, which only the console keeps: it opens pages and files there.
DISPLAY = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


@dataclass(frozen=True, slots=True)
class Staged:
    """What a process that runs from a sealed copy knows of it."""

    stage: Path
    sha: str
    checkout: Path | None  # the owner's factory checkout it was sealed from; None for a release
    release: Path | None  # the release folder; None for a checkout


def staged(environ: Mapping[str, str]) -> Staged | None:
    """The sealed copy this process runs from; None when it runs from a checkout."""
    if STAGE not in environ:
        return None
    checkout, release = environ.get(CHECKOUT), environ.get(RELEASE)
    return Staged(Path(environ[STAGE]), environ[SHA], Path(checkout) if checkout else None,
                  Path(release) if release else None)  # fmt: skip


def release_folder(stage: Path) -> Path:
    """Beside a stage made from a release: its manifest, which no step copies."""
    return stage.with_name(stage.name + ".release")


def stage_commit(host: Host, commit: str, owner: str, checkout: Path) -> tuple[str, Path]:
    """``commit`` of the owner's ``checkout``, sealed under the stage root: its full sha and its folder."""
    git = ["git", "-C", str(checkout)]
    code, out = host.query([*git, "rev-parse", "--verify", f"{commit}^{{commit}}"], user=owner)
    if code != 0:
        raise refuse(f"{commit} is not a commit in {checkout}")
    sha = out.strip()
    stage = layout.STAGE_ROOT / f"{sha}-{secrets.token_hex(4)}"
    host.mkdir(layout.STAGE_ROOT, mode=0o755)
    if host.exists(stage):
        raise refuse(f"{stage} is there already: a sealed copy is always a new folder")
    host.mkdir(stage, mode=0o755)
    try:
        # The owner only reads their checkout; root alone writes the copy, so only root could change it.
        host.pipe([*git, "archive", "--format=tar", sha], owner,
                  ["tar", "-x", "-f", "-", "-C", str(stage), "--no-same-owner", "--no-same-permissions"])  # fmt: skip
        host.run(["chmod", "-R", "a=rX", str(stage)])
    except BaseException:
        host.remove(stage)
        raise
    return sha, stage


def declared_env(inherited: Mapping[str, str], sha: str, stage: Path, *, checkout: Path | None, release: Path | None,
                 display: tuple[str, ...]) -> dict[str, str]:  # fmt: skip
    """Everything a sealed copy starts with; of ``inherited``, only the owner's name, TERM and ``display``."""
    env = {**BASE_ENV, "SUDO_USER": inherited["SUDO_USER"], STAGE: str(stage), SHA: sha}
    env.update({key: str(path) for key, path in ((CHECKOUT, checkout), (RELEASE, release)) if path is not None})
    env.update({key: inherited[key] for key in ("TERM", *display) if key in inherited})
    return env


def exec_staged(sha: str, stage: Path, script: str, argv: list[str], *, checkout: Path | None = None,
                release: Path | None = None, display: tuple[str, ...] = ()) -> NoReturn:  # fmt: skip
    """Run ``script`` (a path inside the factory) again from the sealed copy: from then on every line of factory code
    is the commit's own. ``display`` names the variables of the owner's desktop session it keeps, when set."""
    env = declared_env(os.environ, sha, stage, checkout=checkout, release=release, display=display)
    # -B: root writes past missing write bits, so Python must not write its bytecode into the sealed copy.
    os.execve(layout.PYTHON, [layout.PYTHON, "-I", "-B", str(stage / script), *argv], env)


def finish(host: Host, stage: Path | None, *, kept: bool) -> None:
    """The sealed copy goes when the install or the console that ran from it ends, unless it is ``kept`` beside a
    failed install."""
    if stage is None or host.dry:
        return
    if stage.parent != layout.STAGE_ROOT:
        raise refuse(f"{stage} is not a sealed copy in {layout.STAGE_ROOT}, so it is left in place")
    if kept:
        host.say(f"the sealed copy it ran from stays in {stage}, beside the failed install")
        return
    host.remove(stage)
    if host.exists(release_folder(stage)):
        host.remove(release_folder(stage))
    host.query(["rmdir", "--ignore-fail-on-non-empty", str(layout.STAGE_ROOT)])  # the last copy takes its folder along
