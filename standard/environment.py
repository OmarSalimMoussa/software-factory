"""The process environment, read in one place for the standard build and deploy commands, which read no variable
themselves. Standard library only: CI and the server run these commands without the factory's runtime.

Each read is required or optional by design, and None says what is absent: secrets, each named repo secret the CI
job sets to a value (a name left unset or empty is absent). A command that only hands its environment on takes it
from handed_on().
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def handed_on() -> dict[str, str]:
    return dict(os.environ)


def secrets(names: Sequence[str]) -> dict[str, str]:
    return {name: os.environ[name] for name in names if os.environ.get(name)}
