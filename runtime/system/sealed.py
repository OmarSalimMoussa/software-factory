"""The sealed copy an install or the console runs from: one commit of the owner's checkout, unpacked by root into a
fresh root-owned folder and made read-only before anything runs from it. Nothing in the checkout's working files, and
nothing in the environment the owner started it from, can change what runs: the copy starts again as `python3 -I`
with an environment written out in full.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from runtime.system import layout
from runtime.system.host import Host
from runtime.system.job import refuse

# Set only in the environment of a process that runs from a sealed copy: the copy, its commit and the owner's checkout
# it came from.
STAGE, SHA, CHECKOUT = "SOFTWARE_INSTALL_STAGE", "SOFTWARE_INSTALL_SHA", "SOFTWARE_INSTALL_CHECKOUT"
# What a sealed copy starts with, beside the three above, the owner's name and their terminal's type.
BASE_ENV = {"PATH": layout.ROOT_PATH, "LANG": "C.UTF-8", "HOME": "/root"}


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


def declared_env(inherited: Mapping[str, str], sha: str, stage: Path, checkout: Path,
                 display: tuple[str, ...]) -> dict[str, str]:  # fmt: skip
    """Everything a sealed copy starts with; of ``inherited``, only the owner's name, TERM and ``display``."""
    env = {**BASE_ENV, "SUDO_USER": inherited["SUDO_USER"], STAGE: str(stage), SHA: sha, CHECKOUT: str(checkout)}
    env.update({key: inherited[key] for key in ("TERM", *display) if key in inherited})
    return env


def exec_staged(sha: str, stage: Path, script: str, argv: list[str], *, checkout: Path,
                display: tuple[str, ...] = ()) -> NoReturn:  # fmt: skip
    """Run ``script`` (a path inside the factory) again from the sealed copy: from then on every line of factory code
    is the commit's own. ``display`` names the variables of the owner's desktop session it keeps, when set."""
    env = declared_env(os.environ, sha, stage, checkout, display)
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
        host.say(f"The sealed copy it ran from stays in {stage}, beside the failed install.")
        return
    host.remove(stage)
