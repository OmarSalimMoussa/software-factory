"""The process environment, read in one place for the factory's own programs, which read no variable themselves.

Each read is required or optional by design, and None says what is absent: sudo_user, the owner who ran the command
with sudo (None without sudo); staged, the sealed copy the process runs from (None from a checkout); desktop_session,
the owner's display as sudo kept it (empty when none); claude_session_id, the session Claude Code started the program
for; systemd_result and systemd_exit_status, systemd's word on how a unit's service ended and its main process's exit
status (set only when that process exited); credentials_folder, the credentials systemd loaded for the unit;
take_notify_socket, systemd's notification socket, taken out of the environment so no program the service starts
inherits it. A program that only hands its whole environment on to a child takes it from handed_on().

The sealed copy's hand-off: STAGE and SHA name the copy and its commit; CHECKOUT or RELEASE says where it came from,
the owner's checkout or a release file whose manifest waits in the release folder beside the copy. DISPLAY names the
desktop session's variables, which only the install console keeps.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STAGE, SHA = "SOFTWARE_INSTALL_STAGE", "SOFTWARE_INSTALL_SHA"
CHECKOUT, RELEASE = "SOFTWARE_INSTALL_CHECKOUT", "SOFTWARE_INSTALL_RELEASE"
DISPLAY = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


@dataclass(frozen=True, slots=True)
class Staged:
    stage: Path
    sha: str
    checkout: Path | None
    release: Path | None


def handed_on() -> dict[str, str]:
    return dict(os.environ)


def on_ci() -> bool:
    return os.environ.get("CI") == "true"


def sudo_user() -> str | None:
    return os.environ.get("SUDO_USER")


def staged() -> Staged | None:
    stage = os.environ.get(STAGE)
    if stage is None:
        return None
    checkout, release = os.environ.get(CHECKOUT), os.environ.get(RELEASE)
    return Staged(Path(stage), os.environ[SHA], Path(checkout) if checkout else None,
                  Path(release) if release else None)  # fmt: skip


def desktop_session() -> dict[str, str]:
    return {key: os.environ[key] for key in DISPLAY if key in os.environ}


def claude_session_id() -> str | None:
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


def systemd_result() -> str | None:
    return os.environ.get("SERVICE_RESULT")


def systemd_exit_status() -> str | None:
    return os.environ.get("EXIT_STATUS")


def credentials_folder() -> str | None:
    return os.environ.get("CREDENTIALS_DIRECTORY")


def take_notify_socket() -> str | None:
    return os.environ.pop("NOTIFY_SOCKET", None)
