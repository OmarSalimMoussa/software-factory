from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from standard import config as settings
from standard.config_records import Config
from standard.release import STORE_SECRETS, ReleaseError, release_all

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product" / "factory.toml"
SHA = "0123456789abcdef0123456789abcdef01234567"
SECRETS = {secret: f"value-of-{name}" for name, secret in STORE_SECRETS}


def with_release(live: bool) -> Config:
    release = f"""
[[release]]
kind = "chrome-web-store"
when_changed = ["web/"]
package = "scripts/package-extension"
live = {str(live).lower()}
"""
    return settings.parse_text(FIXTURE.read_text() + release)


class Runner:
    """Records each command; git diff answers ``diff``, the package hook writes its zip."""

    def __init__(self, diff: int) -> None:
        self.diff = diff
        self.calls: list[tuple[list[str], Mapping[str, str]]] = []

    def __call__(self, argv: list[str], cwd: Path, env: Mapping[str, str]) -> int:
        self.calls.append((argv, env))
        if argv[0] == "git":
            return self.diff
        if argv[0].endswith("package-extension"):
            Path(argv[3]).write_bytes(b"zip")
        return 0

    def programs(self) -> list[str]:
        return [Path(argv[0]).name for argv, _ in self.calls]


def release(config: Config, runner: Runner, released: str, environ: Mapping[str, str]) -> list[str]:
    said: list[str] = []
    ok = release_all(
        config,
        Path("/repo"),
        sha=SHA,
        released=released,
        run_number=7,
        run=runner,
        credentials={key: value for key, value in environ.items() if key in SECRETS},
        inherited=environ,
        say=said.append,
    )
    assert ok
    return said


@pytest.mark.parametrize(
    ("live", "released", "diff", "programs", "line"),
    [
        (False, "abc1234", 1, [], "skipped, not live yet"),
        (True, "abc1234", 0, ["git"], "skipped, nothing in web/ changed since abc1234"),
        (True, "abc1234", 1, ["git", "package-extension", "npx"], "published version 1.0.7"),
        (True, "-", 0, ["package-extension", "npx"], "published version 1.0.7"),
    ],
)
def test_a_live_release_publishes_only_what_changed_since_the_last_releases_finished(
    live: bool, released: str, diff: int, programs: list[str], line: str
) -> None:
    runner = Runner(diff)
    environ = {"PATH": "/usr/bin", **SECRETS} if live else {"PATH": "/usr/bin"}

    said = release(with_release(live), runner, released, environ)

    assert runner.programs() == programs
    assert len(said) == 1 and line in said[0]


def test_a_missing_store_secret_fails_before_anything_runs() -> None:
    runner = Runner(1)
    environ = {k: v for k, v in SECRETS.items() if k != "EXTENSION_STORE_REFRESH_TOKEN"}

    with pytest.raises(ReleaseError, match="EXTENSION_STORE_REFRESH_TOKEN"):
        release(with_release(True), runner, "abc1234", environ)
    assert runner.calls == []


def test_the_store_upload_gets_its_credentials_and_the_package_hook_never_sees_them() -> None:
    runner = Runner(1)
    config = with_release(True)

    release(config, runner, "abc1234", {"PATH": "/usr/bin", **SECRETS})

    _, (hook, hook_env), (upload, upload_env) = runner.calls
    assert hook[1:3] == ["1.0.7", config.workspace("prod").origin]
    assert not set(SECRETS) & set(hook_env)
    assert upload[:3] == ["npx", "--yes", "chrome-webstore-upload-cli@4"]
    assert {name: upload_env[name] for name, _ in STORE_SECRETS} == {
        name: f"value-of-{name}" for name, _ in STORE_SECRETS
    }
