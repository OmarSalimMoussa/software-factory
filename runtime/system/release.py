"""The factory as one file to download and run: software-factory.pyz, a Python zip application of one commit that the
factory's CI passed. CI builds it, after every check passed, with

    python3 runtime/system/release.py --commit <sha> --branch <default branch> --repo <owner/name> --out <file>

which also writes the file's SHA-256 beside it. The file holds the factory's files at the commit (what git archive
gives) and release.json: the commit, its subject, the build time, and a SHA-256 of the files. Run with sudo, it checks
that hash, unpacks the files into a new sealed stage, and runs the install console from there; ``--terminal`` runs
the terminal installer and ``--uninstall`` the uninstaller instead. ``--version`` and ``--check`` need no sudo.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import secrets
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared import environment  # noqa: E402 - the path line above must run first
from runtime.system import layout, sealed  # noqa: E402

NAME = "software-factory.pyz"
MANIFEST, TREE, MAIN = "release.json", "factory/", "__main__.py"
FILE, EXEC, LINK = "file", "exec", "link"
_MODES = {FILE: 0o100644, EXEC: 0o100755, LINK: 0o120777}
# Every member's time: two builds of one commit then differ only in release.json's build time.
_STAMP = (1980, 1, 1, 0, 0, 0)
# The zip's own entry point: the factory's code inside, under factory/, is what runs.
LAUNCHER = """import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "factory"))

from runtime.system import release  # noqa: E402

sys.exit(release.launch(HERE, sys.argv[1:]))
"""


class ReleaseError(Exception):
    """The release file cannot be built, read or trusted."""


@dataclass(frozen=True, slots=True)
class Entry:
    path: str  # relative to the tree, with forward slashes
    kind: str  # FILE, EXEC or LINK
    data: bytes  # a link's target, for a link


def tree_digest(entries: list[Entry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda each: each.path):
        digest.update(f"{entry.kind}\0{entry.path}\0{len(entry.data)}\0".encode())
        digest.update(entry.data)
    return digest.hexdigest()


# ---- building it ----------------------------------------------------------------------------------------------------


def _git(checkout: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, check=False)
    if done.returncode != 0:
        raise ReleaseError(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()[-300:]}")
    return done.stdout


def _archived(checkout: Path, sha: str) -> list[Entry]:
    entries = []
    with tarfile.open(fileobj=io.BytesIO(_git(checkout, "archive", "--format=tar", sha))) as tar:
        for member in tar.getmembers():
            if member.isdir() or member.name == "pax_global_header":
                continue
            if member.issym():
                entries.append(Entry(member.name, LINK, member.linkname.encode()))
            elif member.isfile():
                read = tar.extractfile(member)
                if read is None:
                    raise ReleaseError(f"git archive gave no content for {member.name}")
                entries.append(Entry(member.name, EXEC if member.mode & 0o111 else FILE, read.read()))
            else:
                raise ReleaseError(f"git archive holds {member.name}, which is neither a file nor a link")
    return entries


def build(checkout: Path, commit: str, branch: str, repo: str, out: Path) -> dict[str, Any]:
    """Write the release of ``commit`` to ``out`` and its SHA-256 beside it; its manifest."""
    sha = _git(checkout, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    entries = _archived(checkout, sha)
    manifest = {
        "commit": sha,
        "subject": _git(checkout, "log", "-1", "--format=%s", sha).decode().strip(),
        "built": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "branch": branch,
        "repo": repo,
        "tree_sha256": tree_digest(entries),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        fh.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
            _add(zf, MAIN, LAUNCHER.encode(), FILE)
            _add(zf, MANIFEST, (json.dumps(manifest, indent=1) + "\n").encode(), FILE)
            for entry in sorted(entries, key=lambda each: each.path):
                _add(zf, TREE + entry.path, entry.data, entry.kind)
    out.chmod(0o755)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    return manifest


def _add(zf: zipfile.ZipFile, name: str, data: bytes, kind: str) -> None:
    info = zipfile.ZipInfo(name, date_time=_STAMP)
    info.external_attr = _MODES[kind] << 16
    info.compress_type = zipfile.ZIP_STORED if kind == LINK else zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


# ---- reading and trusting it ----------------------------------------------------------------------------------------


def _safe(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and "\\" not in path and all(p not in ("", ".", "..") for p in parts)


class Archive:
    """A release file, read once into memory: what is checked is exactly what is unpacked."""

    def __init__(self, data: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            self.manifest: dict[str, Any] = json.loads(self._zip.read(MANIFEST))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise ReleaseError(f"it is not a release of the factory ({type(exc).__name__})") from exc
        wanted = ("commit", "subject", "built", "branch", "repo", "tree_sha256")
        missing = [key for key in wanted if not isinstance(self.manifest.get(key), str)]
        if missing:
            raise ReleaseError(f"its manifest lacks {', '.join(missing)}")

    @classmethod
    def load(cls, path: Path) -> Archive:
        try:
            return cls(path.read_bytes())
        except OSError as exc:
            raise ReleaseError(f"{path} cannot be read ({type(exc).__name__})") from exc

    @property
    def sha(self) -> str:
        return str(self.manifest["commit"])

    def entries(self) -> list[Entry]:
        found = []
        for info in self._zip.infolist():
            if not info.filename.startswith(TREE) or info.is_dir():
                continue
            mode = info.external_attr >> 16
            kind = LINK if mode == _MODES[LINK] else EXEC if mode & 0o111 else FILE
            found.append(Entry(info.filename.removeprefix(TREE), kind, self._zip.read(info)))
        return found

    def problems(self) -> list[str]:
        """Every way the file differs from what its manifest says it holds; empty when it is whole."""
        problems = []
        names = {info.filename for info in self._zip.infolist() if not info.is_dir()}
        known = {MAIN, MANIFEST}
        odd = sorted(n for n in names if n not in known and not (n.startswith(TREE) and _safe(n[len(TREE) :])))
        if odd:
            problems.append(f"it holds files no release holds: {', '.join(odd[:5])}")
        if MAIN not in names or self._zip.read(MAIN) != LAUNCHER.encode():
            problems.append("its starting code is not the release's own")
        entries = self.entries()
        links = [e for e in entries if e.kind == LINK and not _inside(e.path, e.data.decode(errors="replace"))]
        if links:
            problems.append(f"a link points outside the factory: {links[0].path}")
        if tree_digest(entries) != self.manifest["tree_sha256"]:
            problems.append("its files do not match its manifest")
        return problems

    def unpack(self, stage: Path) -> None:
        """The factory's files into the empty folder ``stage``, then every file and folder read-only, as the owner
        that runs it (root, for a real install)."""
        entries = self.entries()
        for entry in sorted((e for e in entries if e.kind != LINK), key=lambda each: each.path):
            path = stage / entry.path
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(entry.data)
            os.chmod(path, 0o555 if entry.kind == EXEC else 0o444)
        for entry in (e for e in entries if e.kind == LINK):
            (stage / entry.path).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            os.symlink(entry.data.decode(), stage / entry.path)
        for folder, _, _ in sorted(os.walk(stage), key=lambda each: -len(each[0])):
            os.chmod(folder, 0o555)

    def keep(self, folder: Path) -> None:
        """The manifest, read-only, in ``folder``: what the install reads beside the files."""
        folder.mkdir(mode=0o755)
        (folder / MANIFEST).write_bytes(self._zip.read(MANIFEST))
        os.chmod(folder / MANIFEST, 0o444)
        os.chmod(folder, 0o555)


def _inside(path: str, target: str) -> bool:
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return not target.startswith("/") and joined != ".." and not joined.startswith("../")


def read_manifest(folder: Path) -> dict[str, Any]:
    """The manifest a sealed release keeps beside its files."""
    found: dict[str, Any] = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    return found


def seal(archive: Archive, root: Path) -> Path:
    """A new sealed stage under ``root`` holding the release's files, with its release folder beside it."""
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    stage = root / f"{archive.sha}-{secrets.token_hex(4)}"
    stage.mkdir(mode=0o755)
    try:
        archive.unpack(stage)
        archive.keep(sealed.release_folder(stage))
    except BaseException:
        for each in (stage, sealed.release_folder(stage)):
            subprocess.run(["chmod", "-R", "u+w", str(each)], capture_output=True, check=False)
            subprocess.run(["rm", "-rf", "--one-file-system", str(each)], capture_output=True, check=False)
        raise
    return stage


# ---- running it -----------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"sudo python3 {NAME}", description="Install, reinstall or uninstall the "
                                     "factory on this computer. With no option, it opens the install console in your "
                                     "browser.")  # fmt: skip
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--terminal", action="store_true", help="install in this terminal instead of the browser")
    what.add_argument("--uninstall", action="store_true", help="remove the factory from this computer")
    what.add_argument("--version", action="store_true", help="say which commit this release holds")
    what.add_argument("--check", action="store_true", help="check this file against its manifest")
    parser.add_argument("--repo", type=Path, help="your checkout of the product the factory works on")
    parser.add_argument("--healthchecks", action="store_true", help="create the Healthchecks.io checks")
    keep = "leave a failed first install in place, to look at it, instead of removing it"
    parser.add_argument("--keep-on-failure", action="store_true", help=keep)
    parser.add_argument("--dry-run", action="store_true", help="go through every step; change nothing")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="a setting to save")
    parser.add_argument("--verbose", action="store_true", help="print every detail the log keeps")
    return parser


def _passed(args: argparse.Namespace, sha: str) -> tuple[str, list[str]]:
    """The program in the stage that runs, and what it is told."""
    repo = ["--repo", str(args.repo.resolve())] if args.repo else []
    common = [*repo, *(["--dry-run"] if args.dry_run else [])]
    if args.uninstall:
        return "runtime/system/uninstall.py", [*common, *(["--verbose"] if args.verbose else [])]
    flags = [*(["--healthchecks"] if args.healthchecks else []), *(["--keep-on-failure"] if args.keep_on_failure
             else []), *(arg for item in args.set for arg in ("--set", item))]  # fmt: skip
    if args.terminal:
        return "runtime/system/install.py", ["--commit", sha, *common, *flags, *(["--verbose"] if args.verbose else [])]
    return "runtime/system/console.py", ["--commit", sha, *common, *flags]


def launch(here: str, argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.terminal and not args.repo:
        parser.error("--terminal needs --repo <your checkout of the product>")
    if args.uninstall and (args.healthchecks or args.keep_on_failure or args.set):
        parser.error("--uninstall takes only --repo, --dry-run and --verbose")
    try:
        archive = Archive.load(Path(here))
        problems = [] if args.version else archive.problems()
    except ReleaseError as exc:
        problems, archive = [str(exc)], None
    if problems or archive is None:
        print(f"This release file is damaged: {'; '.join(problems)}. Download it again.", file=sys.stderr)
        return 1
    made = archive.manifest
    if args.version or args.check:
        print(f"software-factory {made['commit'][:12]} ({made['subject']}), built {made['built']}")
        if args.check:
            print("Its files match its manifest.")
        return 0
    if os.geteuid() != 0 or environment.sudo_user() is None:
        print(f"Run it with sudo, as yourself: sudo python3 {Path(here).name}", file=sys.stderr)
        return 2
    stage = seal(archive, layout.STAGE_ROOT)
    script, passed = _passed(args, archive.sha)
    sealed.exec_staged(archive.sha, stage, script, passed, release=sealed.release_folder(stage),
                       display=() if args.terminal or args.uninstall else environment.DISPLAY)  # fmt: skip


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description="Build the factory's release file.")
    parser.add_argument("--commit", required=True, help="the commit CI passed")
    parser.add_argument("--branch", required=True, help="the factory's default branch, which the commit is on")
    parser.add_argument("--repo", required=True, help="the factory's GitHub repository, as owner/name")
    parser.add_argument("--out", required=True, type=Path, help=f"where to write {NAME}")
    args = parser.parse_args(argv)
    try:
        made = build(Path.cwd(), args.commit, args.branch, args.repo, args.out)
    except ReleaseError as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return 1
    print(f"{args.out}: {made['commit'][:12]} ({made['subject']}), built {made['built']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
