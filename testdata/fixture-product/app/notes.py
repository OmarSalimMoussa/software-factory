"""Notes: adding one and finding them again."""

from __future__ import annotations

from dataclasses import dataclass


class EmptyNoteError(ValueError):
    """A note must hold some text."""


@dataclass(frozen=True)
class Note:
    text: str


def add(notes: list[Note], text: str) -> list[Note]:
    """The notes with a new one first."""
    if not text.strip():
        raise EmptyNoteError("a note needs some text")
    return [Note(text.strip()), *notes]


def find(notes: list[Note], word: str) -> list[Note]:
    wanted = word.casefold()
    return [note for note in notes if wanted in note.text.casefold()]
