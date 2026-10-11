"""The process environment, read in one place for the standard build and deploy commands.

Each function reads what a command takes from the CI job or the SSH login it runs in, required or optional by design,
and says what an absence means. The commands call these and read no variable themselves. Standard library only: CI
and the server run these commands without the factory's runtime.
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def handed_on() -> dict[str, str]:
    """The whole environment, for a command that only hands it on to a program it starts."""
    return dict(os.environ)


def github_output() -> str | None:
    """The file GitHub Actions reads a step's outputs from; None outside GitHub Actions."""
    return os.environ.get("GITHUB_OUTPUT")


def ssh_original_command() -> str | None:
    """What an SSH client asked a forced command to run; None for a login that asked nothing."""
    return os.environ.get("SSH_ORIGINAL_COMMAND")


def secrets(names: Sequence[str]) -> dict[str, str]:
    """Each of ``names`` the CI job sets to a value; a name it leaves unset or empty is absent."""
    return {name: os.environ[name] for name in names if os.environ.get(name)}
