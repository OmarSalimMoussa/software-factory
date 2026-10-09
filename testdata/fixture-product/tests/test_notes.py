from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings
from app.notes import EmptyNoteError, Note, add, find


def test_a_new_note_comes_first() -> None:
    assert add([Note("old")], " new ") == [Note("new"), Note("old")]


def test_an_empty_note_is_refused() -> None:
    with pytest.raises(EmptyNoteError):
        add([], "   ")


def test_find_ignores_case() -> None:
    notes = [Note("Buy milk"), Note("Call Sam")]
    assert find(notes, "MILK") == [Note("Buy milk")]


def test_settings_name_every_missing_key() -> None:
    with pytest.raises(ConfigError, match="NOTES_STORE, NOTES_SYNC_URL"):
        load_settings({})
