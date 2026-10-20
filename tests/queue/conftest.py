from __future__ import annotations

from pathlib import Path

import pytest
from bundle_kit import Product
from queue_kit import World, make_world

from runtime.qa import qa_browser


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


@pytest.fixture
def product(world: World, monkeypatch: pytest.MonkeyPatch) -> Product:
    monkeypatch.setattr(qa_browser, "runs_root", lambda: world.repo.parent / "runs")
    return Product(world)
