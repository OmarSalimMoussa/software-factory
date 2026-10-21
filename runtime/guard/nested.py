"""The command lines a program hands to a shell of its own: `sh -c`, `eval`, `script -c`, a launcher's arguments and
`find -exec`, so the guard reads them like any command. Stdlib only."""

from __future__ import annotations

import re
import shlex

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# A script path that names the shell's own standard input.
_STDIN = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})
_COMMAND_RUNNERS = frozenset({"sudo", "env", "exec", "command", "nohup", "time", "busybox", "nice", "setsid"})
# Where a pipeline ends: `;`, `&&`, `||` or a lone `&`, never the `&` of a redirect (`2>&1`, `&>`).
_PIPELINE_END = re.compile(r"&&|\|\||;|(?<![<>&])&(?![>&])")
_REDIRECT = re.compile(r"^\d*(<<-?|<<<|<&|>&|&>>?|>>|>\||<|>)")


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


def heredoc_shell(line: str, start: int) -> tuple[bool, bool]:
    """Whether the pipeline holding the heredoc at ``start`` runs a shell, and whether that shell reads the body from
    its standard input. A group (`{ …; } <<EOF`) may run one, so its body is read, though no shell is known to take it.
    """
    bounds = [0, *(m.end() for m in _PIPELINE_END.finditer(line))]
    ends = [m.start() for m in _PIPELINE_END.finditer(line) if m.start() > start]
    pipeline = line[max(b for b in bounds if b <= start) : min(ends) if ends else len(line)]
    shell = reads = False
    for stage in pipeline.split("|"):
        words = _command_words(stage)
        if words[:1] in (["}"], [")"]):
            shell = True
        elif words and words[0].rsplit("/", 1)[-1] in SHELLS:
            shell = True
            reads = reads or reads_its_script_from_stdin(words[0].rsplit("/", 1)[-1], words[1:])
    return shell, reads


def _command_words(stage: str) -> list[str]:
    """A stage's words from its program on: past redirects (`<<'A' bash`), assignments, `{`, `!` and leaders."""
    words: list[str] = []
    skip = False
    for word in stage.split():
        if skip:
            skip = False
        elif match := _REDIRECT.match(word):
            skip = match.end() == len(word)  # the redirect's target is the next word
        elif not words and ("=" in word or word in {"{", "(", "!"}):
            continue
        else:
            words.append(word)
    while words and words[0].rsplit("/", 1)[-1] in _COMMAND_RUNNERS:
        words = [w for w in words[1:] if not w.startswith("-")]
    return words
