"""The releases after a prod deploy: each live ``[[release]]`` of factory.toml whose files changed since the commit
prod ran before is packaged by the product's hook and published.

Run by deploy-prod.yml's releases job:
``python3 -m standard.release --repo . --sha <40-hex> --previous <commit or -> --run <run number>``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from standard import config as settings
from standard.config import Config, Release

# The chrome-web-store upload's own variable, then the repo secret the workflow hands it in.
STORE_SECRETS = (
    ("EXTENSION_ID", "EXTENSION_STORE_EXTENSION_ID"),
    ("PUBLISHER_ID", "EXTENSION_STORE_PUBLISHER_ID"),
    ("CLIENT_ID", "EXTENSION_STORE_CLIENT_ID"),
    ("CLIENT_SECRET", "EXTENSION_STORE_CLIENT_SECRET"),
    ("REFRESH_TOKEN", "EXTENSION_STORE_REFRESH_TOKEN"),
)
UPLOAD = ["npx", "--yes", "chrome-webstore-upload-cli@4"]

Run = Callable[[list[str], Path, Mapping[str, str]], int]


def real_run(argv: list[str], cwd: Path, env: Mapping[str, str]) -> int:
    """No shell; the output goes straight to CI's log."""
    return subprocess.run(argv, cwd=cwd, env=dict(env), check=False).returncode


class ReleaseError(Exception):
    pass


def missing_secrets(config: Config, environ: Mapping[str, str]) -> list[str]:
    if not any(release.live for release in config.releases):
        return []
    return [secret for _, secret in STORE_SECRETS if not environ.get(secret)]


def changed(release: Release, repo: Path, *, sha: str, previous: str, run: Run, environ: Mapping[str, str]) -> bool:
    if previous == "-":
        return True
    code = run(["git", "diff", "--quiet", previous, sha, "--", *release.when_changed], repo, environ)
    if code not in (0, 1):
        raise ReleaseError(f"git could not compare {previous} with {sha} (exit code {code})")
    return code == 1


def release_all(
    config: Config,
    repo: Path,
    *,
    sha: str,
    previous: str,
    run_number: int,
    run: Run,
    environ: Mapping[str, str],
    say: Callable[[str], None],
) -> bool:
    """Prints one line per release; False when any release failed."""
    missing = missing_secrets(config, environ)
    if missing:
        raise ReleaseError(f"the repo secrets {', '.join(missing)} are not set; a live release needs every one")
    if not config.releases:
        say("release: factory.toml names no releases")
    version = f"1.0.{run_number}"
    origin = config.workspace("prod").origin
    # The product's packaging hook never sees the store's credentials.
    hook_env = {key: value for key, value in environ.items() if key not in dict(STORE_SECRETS).values()}
    ok = True
    with tempfile.TemporaryDirectory(prefix="factory-release-") as tmp:
        for number, release in enumerate(config.releases, 1):
            label = f"release {number} ({release.kind}, {release.package})"
            if not release.live:
                say(f"{label}: skipped, not live yet (live = false in factory.toml)")
                continue
            if not changed(release, repo, sha=sha, previous=previous, run=run, environ=environ):
                say(f"{label}: skipped, nothing in {', '.join(release.when_changed)} changed since {previous}")
                continue
            package = Path(tmp) / str(number) / "release.zip"
            package.parent.mkdir()
            code = run([str(repo / release.package), version, origin, str(package)], repo, hook_env)
            if code != 0 or not package.is_file():
                said = f"exited with code {code}" if code else f"wrote no {package.name}"
                say(f"{label}: FAILED, the package hook {said}")
                ok = False
                continue
            store_env = {**hook_env, **{name: environ[secret] for name, secret in STORE_SECRETS}}
            code = run([*UPLOAD, "--source", str(package)], repo, store_env)
            if code != 0:
                say(f"{label}: FAILED, the Chrome Web Store upload exited with code {code}")
                ok = False
                continue
            say(f"{label}: published version {version}, built for {origin}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.release", description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--sha", required=True, help="the full commit prod now runs")
    parser.add_argument("--previous", required=True, help="the commit prod ran before, or - when none did")
    parser.add_argument("--run", type=int, required=True, help="the workflow's run number: the version's last part")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("--sha must be a full commit: 40 lowercase hex characters")
    if args.previous != "-" and not re.fullmatch(r"[0-9a-f]{7,40}", args.previous):
        parser.error("--previous must be a commit or -")
    if args.run < 1:
        parser.error("--run must be a whole number above 0")
    repo = args.repo.resolve()
    try:
        ok = release_all(
            settings.load(repo),
            repo,
            sha=args.sha,
            previous=args.previous,
            run_number=args.run,
            run=real_run,
            environ=os.environ,
            say=lambda line: print(line, flush=True),
        )
    except settings.ConfigError as exc:
        print("release: FAIL: factory.toml: " + "; ".join(exc.problems), file=sys.stderr)
        return 1
    except ReleaseError as exc:
        print(f"release: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
