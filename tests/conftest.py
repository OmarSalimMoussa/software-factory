"""Every test runs as if no install started it: the installer's own variables never reach a test or what it starts."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_install_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.startswith("SOFTWARE_INSTALL_")]:
        monkeypatch.delenv(key)
