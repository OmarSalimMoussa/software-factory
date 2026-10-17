"""The process environment, read in one place for the standard build and deploy commands, which read no variable
themselves. Standard library only: they run without the factory's runtime. A command that only hands its environment
on takes it from handed_on().
"""

from __future__ import annotations

import os


def handed_on() -> dict[str, str]:
    return dict(os.environ)
