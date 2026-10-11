"""The process environment, read in one place for the standard build and deploy commands, which read no variable
themselves. Standard library only: CI and the server run these commands without the factory's runtime.

Each read is required or optional by design, and None says what is absent: github_output, the file GitHub Actions
reads a step's outputs from (None outside GitHub Actions); ssh_original_command, what an SSH client asked a forced
command to run (None for a login that asked nothing); secrets, each named repo secret the CI job sets to a value (a
name left unset or empty is absent). A command that only hands the job's environment on takes it from handed_on().
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def handed_on() -> dict[str, str]:
    return dict(os.environ)


def github_output() -> str | None:
    return os.environ.get("GITHUB_OUTPUT")


def ssh_original_command() -> str | None:
    return os.environ.get("SSH_ORIGINAL_COMMAND")


def secrets(names: Sequence[str]) -> dict[str, str]:
    return {name: os.environ[name] for name in names if os.environ.get(name)}
