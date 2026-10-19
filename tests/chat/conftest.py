from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "queue"))

from chat_kit import CHANNEL, TOKEN, FakeDiscord
from queue_kit import World, make_world

from runtime.chat import discord
from runtime.shared import store


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


@pytest.fixture
def chat(world: World, monkeypatch: pytest.MonkeyPatch) -> FakeDiscord:
    """The owner gave the bot's token and named one channel; Discord is the fake."""
    fake = FakeDiscord(world.clock)
    monkeypatch.setattr(discord, "send", fake.send)
    store.write_text(discord.token_path(world.places), TOKEN + "\n", mode=0o600)
    world.with_settings(chat__channels=[CHANNEL])
    return fake
