"""Prod's build on the box: a clean copy of the commit from the warden's mirror, and each image its plan names that
Docker lacks, built from that copy.

The mirror is the warden's bare repository, which the admin user may read but does not own, so git takes it only with
safe.directory. A build passes the plan's args and nothing else; it never labels an image, as the nightly prune of
slots' images keys on their label and prod's images must stay for a push back; and it pushes nothing.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from standard.plan import Plan
from standard.server import deploy_steps as steps
from standard.server.deploy_steps import DeployError, Say
from standard.services.provision import Run, checked, tail


def git(mirror: Path, *argv: str) -> list[str]:
    return ["git", "-c", "safe.directory=*", f"--git-dir={mirror}", *argv]


def holds(run: Run, mirror: Path, sha: str) -> bool:
    return run(git(mirror, "cat-file", "-e", f"{sha}^{{commit}}"), None).returncode == 0


def read(run: Run, mirror: Path, sha: str, rel: str) -> str:
    done = run(git(mirror, "show", f"{sha}:{rel}"), None)
    if done.returncode != 0:
        raise DeployError(f"{rel} at {sha[: steps.SHORT]} cannot be read: {tail(done.stderr, 1)}")
    return done.stdout


def unpack(run: Run, mirror: Path, sha: str, into: Path) -> Path:
    archive, source = into / "source.tar", into / "source"
    checked(run, git(mirror, "archive", "--format=tar", f"--output={archive}", sha))
    try:
        with tarfile.open(archive) as tar:
            # The data filter refuses a member that would land outside the folder, or a link that points out of it.
            tar.extractall(source, filter="data")
    except tarfile.TarError as exc:
        raise DeployError(f"the commit's files cannot be unpacked: {exc}") from exc
    archive.unlink()
    return source


def images(run: Run, plan: Plan, source: Path, say: Say) -> None:
    """Build each of the plan's images Docker lacks, bases first; DeployError names the first that fails."""
    held = {ref for ref, _ in steps.held_images(run)}
    missing = [image for image in plan.images if f"{image.ref}:{plan.short}" not in held]
    for image in missing:
        tag = f"{image.ref}:{plan.short}"
        argv = ["docker", "build", "--tag", tag, "--file", str(source / image.dockerfile)]
        argv += ["--target", image.target] if image.target else []
        argv += [word for key, value in image.args.items() for word in ("--build-arg", f"{key}={value}")]
        done = run([*argv, str(source / image.context)], None)
        if done.returncode != 0:
            for line in (done.stdout + done.stderr).splitlines():
                say(f"build {image.name}: {line}")
            raise DeployError(
                f"docker build of {tag} failed (exit {done.returncode}): {tail(done.stderr or done.stdout, 3)}"
            )
        say(f"images: built {tag}")
    say(f"images: {len(plan.images) - len(missing)} of {len(plan.images)} were here at {plan.short}")
