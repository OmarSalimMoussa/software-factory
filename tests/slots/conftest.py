from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The queue's test kit builds the same world: a checkout, settings and fakes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "queue"))

from queue_kit import World, make_world  # noqa: E402


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)
