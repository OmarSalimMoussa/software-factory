"""The words before a program that only say how it runs (`uv run`, `timeout`, `command`, `function f {`), read off
so the guard sees the program itself. Each takes the program's own words and gives back the command it runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.guard import shell


def plain(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[shell.Invocation],
    depth: int,
) -> list[str]:
    return rest


def timeout(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[shell.Invocation],
    depth: int,
) -> list[str]:
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in {"-s", "--signal", "-k", "--kill-after"} and rest:
            rest.pop(0)
    return rest[1:]  # past the duration


def function(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[shell.Invocation],
    depth: int,
) -> list[str]:
    """``function f { …`` defines f: its body runs, so what follows the name and the brace is read."""
    rest = rest[1:]
    return rest[1:] if rest[:1] == ["{"] else rest


def command(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[shell.Invocation],
    depth: int,
) -> list[str]:
    """``command -p x`` runs x; ``command -v x`` and ``-V`` only say where x is."""
    flags = []
    while rest and rest[0].startswith("-"):
        flags.append(rest.pop(0))
    return [] if {"-v", "-V"} & set(flags) else rest


# uv's own flags that take a value, before the program `uv run` runs.
_UV_VALUED = frozenset(
    {
        "--extra",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--project",
        "--directory",
        "--group",
        "--only-group",
        "--package",
        "--env-file",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-f",
        "-w",
        "--from",
        "--no-extra",
        "--no-group",
        "--cache-dir",
        "--config-file",
        "--color",
        "--python-preference",
        "--resolution",
        "--prerelease",
        "--exclude-newer",
        "--link-mode",
        "--config-setting",
        "-C",
        "--keyring-provider",
        "--allow-insecure-host",
        "--refresh-package",
        "--upgrade-package",
        "-P",
        "--reinstall-package",
    }
)


def uv(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[shell.Invocation],
    depth: int,
) -> list[str]:
    """``uv run`` runs the program after uv's own flags, before or after ``run`` (`uvx` the tool named first); uv's
    other commands run none."""
    words = list(rest)
    if program == "uv":
        words = _past_flags(words)
        if words[:1] != ["run"]:
            return []
        words = words[1:]
    return _past_flags(words)


def _past_flags(words: list[str]) -> list[str]:
    while words and words[0].startswith("-"):
        flag = words.pop(0)
        if flag == "--":
            break
        if flag in _UV_VALUED and words:
            words.pop(0)
    return words
