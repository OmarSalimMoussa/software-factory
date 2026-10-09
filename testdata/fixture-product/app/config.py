"""The app's settings, read once at start; every key is required."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

KEYS = ("NOTES_STORE", "NOTES_SYNC_URL")


class ConfigError(Exception):
    """The settings cannot be used; the message lists every missing key."""


@dataclass(frozen=True)
class Settings:
    store: str
    sync_url: str


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    missing = [key for key in KEYS if not environ.get(key)]
    if missing:
        raise ConfigError("missing settings: " + ", ".join(missing))
    return Settings(environ["NOTES_STORE"], environ["NOTES_SYNC_URL"])
