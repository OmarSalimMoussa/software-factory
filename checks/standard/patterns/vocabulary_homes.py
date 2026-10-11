"""Vocabulary homes: each listed vocabulary is written only in its home folder.

A vocabulary (tool names, event kinds, status words) spelled outside its home is a copy nothing keeps in step. Every
tracked text file is read, comments and Markdown included. A word matches with no letter or digit before it (an
underscore may precede it, so a ``prefix_<word>`` spelling still counts) and no word character after it. An allowed
file must spell a word and carries the grounds that keep it in step, such as the test that checks it.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo
from checks.standard.patterns import engine

ID = "vocabulary-homes"
RULE = "Listed words are written only in their home folder"
VALUES_FILE = "vocabulary-homes.toml"
VALUES = {
    "vocabularies": [{"name": str, "words": [str], "home": [str], "suffixes": [str], "allow": {str: str},
                      "remedy": str}]
}  # fmt: skip


def word_pattern(words: list[str]) -> re.Pattern[str]:
    ordered = sorted(set(words), key=len, reverse=True)
    return re.compile(rf"(?<![A-Za-z0-9])({'|'.join(re.escape(w) for w in ordered)})(?![A-Za-z0-9_])")


def _vocabulary(repo: Repo, vocabulary: dict[str, Any]) -> list[str]:
    label = f"{ID} ({vocabulary['name']})"
    if not vocabulary["words"] or any(not w.strip() for w in vocabulary["words"]):
        return [f"{label}: list at least one word, and no empty word"]
    problems = engine.missing(repo, label, [*vocabulary["home"], *vocabulary["allow"]])
    pattern = word_pattern(vocabulary["words"])
    suffixes = tuple(vocabulary["suffixes"])
    answered: set[str] = set()
    for rel in sorted(repo.tracked):
        if scope.under(rel, vocabulary["home"]) or engine.out_of_scope(rel):
            continue
        if suffixes and not rel.endswith(suffixes):
            continue
        hits = [(n, m.group(1)) for n, line in enumerate(repo.read(rel).splitlines(), 1)
                for m in pattern.finditer(line)]  # fmt: skip
        if rel in vocabulary["allow"]:
            if hits:
                answered.add(rel)
            continue
        problems += [f"{rel}:{n}: {vocabulary['name']} word {word!r} written outside its home — "
                     f"{vocabulary['remedy']}" for n, word in hits]  # fmt: skip
    problems += [
        f"{label}: stale exemption: {rel} spells no word; remove it from allow"
        for rel in sorted(set(vocabulary["allow"]) - answered)
        if (repo.root / rel).exists()
    ]
    return problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return [p for vocabulary in values["vocabularies"] for p in _vocabulary(repo, vocabulary)]
