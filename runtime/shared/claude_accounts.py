"""The agents' Claude accounts as files: the rules the installer and the warden share.

Each account's sign-in token is <secrets>/claude-accounts/<name>.token, and ``active`` beside them names the one the
agents use. <secrets>/claude.token is a copy of the active token: the file the agent units load, so the units never
change. A token is never printed, logged, answered or put in an error; only its last characters are shown, to tell
accounts apart.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT = "main"
UNIT_TOKEN = "claude.token"
SHOWN = 4  # the characters of a token that may be shown
_FOLDER, _SUFFIX = "claude-accounts", ".token"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
# Printable ASCII with no space, as `claude setup-token` prints it. Anything shorter is a paste gone wrong, and its
# last characters would show most of it.
_TOKEN = re.compile(r"[\x21-\x7e]{16,}")


class AccountError(ValueError):
    """A name or a token that cannot be an account's; the message never holds the token."""


def folder(secrets: Path) -> Path:
    return secrets / _FOLDER


def token_file(secrets: Path, name: str) -> Path:
    return folder(secrets) / f"{name}{_SUFFIX}"


def token_files(secrets: Path) -> list[Path]:
    return sorted(folder(secrets).glob(f"*{_SUFFIX}"))


def name_of(path: Path) -> str | None:
    """The account a token file holds, or None when its file name is not an account's."""
    stem = path.name.removesuffix(_SUFFIX)
    return stem if path.name.endswith(_SUFFIX) and _NAME.fullmatch(stem) else None


def check_name(name: object) -> str:
    # fullmatch: with $, a name ending in a line break would pass and become part of a file name.
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise AccountError(
            "an account's name is 1 to 31 lower-case letters, digits and dashes, starting with a letter or digit"
        )
    return name


def check_token(raw: str) -> str:
    token = raw.strip()
    if not _TOKEN.fullmatch(token):
        raise AccountError(
            "a token is one line of at least 16 characters with no spaces: paste what `claude setup-token` printed"
        )
    return token
