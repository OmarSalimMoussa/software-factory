"""The sealed copy an install or an uninstall runs from: one commit, from the owner's checkout or from GitHub's
tarball of it, unpacked by root into a fresh root-owned folder and made read-only before anything runs from it.
Nothing in the checkout's working files, and nothing in the environment the owner started it from, can change what
runs: the copy starts again as `python3 -I` with an environment written out in full.
"""

from __future__ import annotations

import io
import os
import secrets
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from runtime.shared import environment
from runtime.shared.environment import CHECKOUT, SHA, STAGE
from runtime.system import layout
from runtime.system.host import Host
from runtime.system.job import refuse

# What a sealed copy starts with, beside the hand-off variables, the owner's name and their terminal's type.
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


def unpack(data: bytes, sha: str, root: Path) -> Path:
    """GitHub's tarball of ``sha``, unpacked into a new sealed copy under ``root``: its folder. Git names the commit in
    the tarball's own header, and every file sits in one top folder, which the copy leaves out."""
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    stage = root / f"{sha}-{secrets.token_hex(4)}"
    stage.mkdir(mode=0o755)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            found = tar.getmembers()
            named = tar.pax_headers.get("comment") if tar.pax_headers is not None else None
            if named != sha or len({m.name.split("/", 1)[0] for m in found}) != 1:
                raise refuse(f"GitHub sent no tarball of {sha[:12]} as git makes one")
            members = [m for m in found if "/" in m.name.rstrip("/")]
            for member in members:
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise refuse(f"GitHub's tarball of {sha[:12]} holds {member.name}, not a file, folder or link")
                member.name = member.name.split("/", 1)[1]
            tar.extractall(stage, members=members, filter="data")
        subprocess.run(["chmod", "-R", "a=rX", str(stage)], capture_output=True, check=True)
    except BaseException:
        discard(stage)
        raise
    return stage


def discard(stage: Path) -> None:
    """A sealed copy removed by the program that made it, as root removes any folder: read-only ones too."""
    subprocess.run(["chmod", "-R", "u+w", str(stage)], capture_output=True, check=False)
    subprocess.run(["rm", "-rf", "--one-file-system", str(stage)], capture_output=True, check=False)


def declared_env(inherited: Mapping[str, str], sha: str, stage: Path, *, checkout: Path | None) -> dict[str, str]:
    """Everything a sealed copy starts with; of ``inherited``, only the owner's name (none when the upgrade unit runs
    it, as root itself) and TERM."""
    env = {**BASE_ENV, STAGE: str(stage), SHA: sha}
    env.update({key: inherited[key] for key in ("SUDO_USER", "TERM") if key in inherited})
    if checkout is not None:
        env[CHECKOUT] = str(checkout)
    return env


def _argv(stage: Path, script: str, argv: list[str]) -> list[str]:
    # -B: root writes past missing write bits, so Python must not write its bytecode into the sealed copy.
    return [layout.PYTHON, "-I", "-B", str(stage / script), *argv]


def exec_staged(sha: str, stage: Path, script: str, argv: list[str], *, checkout: Path) -> NoReturn:
    """Run ``script`` (a path inside the factory) again from the sealed copy: from then on every line of factory code
    is the commit's own."""
    env = declared_env(environment.handed_on(), sha, stage, checkout=checkout)
    os.execve(layout.PYTHON, _argv(stage, script, argv), env)


def run_staged(sha: str, stage: Path, script: str, argv: list[str]) -> int:
    """Run ``script`` from the sealed copy of a commit GitHub sent, as root's upgrade unit does, and wait: its exit."""
    env = declared_env({}, sha, stage, checkout=None)
    return subprocess.run(_argv(stage, script, argv), env=env, check=False).returncode


def finish(host: Host, stage: Path | None, *, kept: bool) -> None:
    """The sealed copy goes when the install that ran from it ends, unless it is ``kept`` beside a failed install."""
    if stage is None or host.dry:
        return
    if stage.parent != layout.STAGE_ROOT:
        raise refuse(f"{stage} is not a sealed copy in {layout.STAGE_ROOT}, so it is left in place")
    if kept:
        host.say(f"the sealed copy it ran from stays in {stage}, beside the failed install")
        return
    host.remove(stage)
    host.query(["rmdir", "--ignore-fail-on-non-empty", str(layout.STAGE_ROOT)])  # the last copy takes its folder along
