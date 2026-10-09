"""The command lines a program hands to a shell of its own: `sh -c`, `eval`, `script -c`, a launcher's arguments and
`find -exec`, so the guard reads them like any command. Stdlib only."""

from __future__ import annotations

import shlex

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# A script path that names the shell's own standard input.
_STDIN = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})


def _shell_script(rest: list[str]) -> list[str]:
    for i, arg in enumerate(rest):
        if arg.startswith("-") and "c" in arg.lstrip("-") and not arg.startswith("--") and i + 1 < len(rest):
            return [rest[i + 1]]
    return []


def _script_command(rest: list[str]) -> list[str]:
    for i, arg in enumerate(rest):
        if arg in {"-c", "--command"} and i + 1 < len(rest):
            return [rest[i + 1]]
    return []


def _every_candidate(rest: list[str]) -> list[str]:
    # Any argument may be a whole command line (tmux), or the start of one spread over the rest (systemd-run,
    # screen). Reading every candidate is safe: a stray word only reads as a harmless program.
    candidates: list[str] = []
    for i, arg in enumerate(rest):
        if not arg.startswith("-"):
            candidates.append(arg)
            candidates.append(shlex.join(rest[i:]))
    return candidates


_LAUNCHERS = frozenset(
    {
        "tmux",
        "screen",
        "systemd-run",
        "at",
        "batch",
        "watch",
        "nsenter",
        "unshare",
        "flatpak-spawn",
    }
)


def scripts(program: str, rest: list[str]) -> list[str]:
    """The command strings a program would hand to a shell: ``sh -c``, ``eval``, ``tmux``, ``screen``, ``script -c``."""
    if program in SHELLS:
        return _shell_script(rest)
    if program == "eval":
        return [" ".join(rest)]
    if program == "script":
        return _script_command(rest)
    if program in _LAUNCHERS:
        return _every_candidate(rest)
    if program == "find":
        return _find_commands(rest)
    return []


def _find_commands(rest: list[str]) -> list[str]:
    """The commands ``find -exec`` (and ``-execdir``, ``-ok``, ``-okdir``) runs, each up to its ``;`` or ``+``."""
    commands: list[str] = []
    words = iter(rest)
    for word in words:
        if word in {"-exec", "-execdir", "-ok", "-okdir"}:
            argv = []
            for part in words:
                if part in {";", "+"}:
                    break
                argv.append(part)
            commands.append(shlex.join(argv))
    return commands


def reads_its_script_from_stdin(program: str, rest: list[str]) -> bool:
    """A shell given no `-c` and no script file reads its commands from a pipe, a file or a heredoc."""
    if program not in SHELLS:
        return False
    if _shell_script(rest):
        return False
    from_stdin = False
    scripts: list[str] = []
    words = iter(rest)
    for word in words:
        if word in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            next(words, None)
        elif word == "--":
            scripts.extend(words)
        elif word.startswith("-") and not word.startswith("--"):
            # `-s` reads the script from standard input, whatever arguments follow.
            from_stdin = from_stdin or "s" in word[1:]
        elif not word.startswith(("-", "+")):
            scripts.append(word)
    return from_stdin or not scripts or scripts[0] in _STDIN
