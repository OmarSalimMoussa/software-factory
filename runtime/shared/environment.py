"""The process environment, read in one place for the factory's own programs.

Each function reads what a program takes from the process it runs in: sudo's owner, systemd's word on a stop, the
sealed copy's hand-off, Claude Code's session. Each read is required or optional by design, and says what an absence
means. Programs call these and read no variable themselves; a program that only hands its whole environment on to a
child it starts takes it from handed_on().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Set only in the environment of a process that runs from a sealed copy: the copy, its commit, and where it came from:
# the owner's checkout, or a release file whose manifest and history wait in the release folder beside the copy.
STAGE, SHA = "SOFTWARE_INSTALL_STAGE", "SOFTWARE_INSTALL_SHA"
CHECKOUT, RELEASE = "SOFTWARE_INSTALL_CHECKOUT", "SOFTWARE_INSTALL_RELEASE"
# The owner's desktop session, which only the install console keeps: it opens pages and files there.
DISPLAY = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


@dataclass(frozen=True, slots=True)
class Staged:
    """What a process that runs from a sealed copy knows of it."""

    stage: Path
    sha: str
    checkout: Path | None  # the owner's factory checkout it was sealed from; None for a release
    release: Path | None  # the release folder; None for a checkout


def handed_on() -> dict[str, str]:
    """The whole environment, for a program that only hands it on to a child it starts."""
    return dict(os.environ)


def on_ci() -> bool:
    """Whether this runs on CI, which sets CI=true."""
    return os.environ.get("CI") == "true"


def sudo_user() -> str | None:
    """The owner who started this command with sudo; None when it runs without sudo."""
    return os.environ.get("SUDO_USER")


def staged() -> Staged | None:
    """The sealed copy this process runs from; None when it runs from a checkout."""
    stage = os.environ.get(STAGE)
    if stage is None:
        return None
    checkout, release = os.environ.get(CHECKOUT), os.environ.get(RELEASE)
    return Staged(Path(stage), os.environ[SHA], Path(checkout) if checkout else None,
                  Path(release) if release else None)  # fmt: skip


def desktop_session() -> dict[str, str]:
    """The owner's desktop session as sudo kept it; empty when sudo kept none."""
    return {key: os.environ[key] for key in DISPLAY if key in os.environ}


def claude_session_id() -> str | None:
    """The session Claude Code started this program for; None when Claude Code did not start it."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


def systemd_result() -> str | None:
    """systemd's word for how a unit's service ended, as its stop commands see it; None when systemd gave none."""
    return os.environ.get("SERVICE_RESULT")


def systemd_exit_status() -> str | None:
    """The exit status of a unit's main process, which systemd sets only when that process exited."""
    return os.environ.get("EXIT_STATUS")


def credentials_folder() -> str | None:
    """The folder of the credentials systemd loaded for this unit; None when it loaded none."""
    return os.environ.get("CREDENTIALS_DIRECTORY")


def take_notify_socket() -> str | None:
    """systemd's notification socket for this service, taken out of the environment, so no program the service
    starts inherits it; None outside systemd."""
    return os.environ.pop("NOTIFY_SOCKET", None)
