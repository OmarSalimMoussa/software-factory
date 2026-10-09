"""What runs is known only once the shell expands a word the guard reads as text: such a command is unreadable.

A program named by a variable or a command's output, an alias, a sourced pipe, and a git or gh word that could
turn out to be a flag (`$a` holding `--no-verify`) fail closed. A message or a path holding `$` is fine.
"""

from __future__ import annotations

import re

from runtime.guard import git_rules, shell
from runtime.guard.facts import Call, Decision, Refusal

# The current directory is no secret: a script named from it (`"$PWD"/scripts/dev.sh`) is known.
_PWD = re.compile(r"^\$(PWD\b|\{PWD\})")
# git commands that only read, or only stage: no expansion in their words skips a hook or moves a branch.
_READ_ONLY = frozenset(
    {"log", "diff", "show", "grep", "rev-parse", "merge-base", "ls-files", "status", "blame", "add", "cat-file",
     "describe", "shortlog", "name-rev"}
)  # fmt: skip
# git's and gh's flags whose value is the next word, so a message or a jq filter may hold `$`.
_VALUED = frozenset(
    {"-m", "-F", "-C", "-c", "--message", "--file", "--author", "--date", "--since", "--until", "--grep", "--format",
     "--pretty", "-S", "-G", "--template", "--jq", "-q", "--title", "--body", "--json"}
)  # fmt: skip


def check(call: Call, inv: shell.Invocation) -> Decision:
    """What runs is known only once the shell expands a word the guard reads as text: a program named by a variable
    or a command's output, an alias, a sourced pipe, or a git or gh word that could turn out a flag."""
    if call.off("unreadable-command") or not inv.argv:
        return None
    reason = ""
    if _expands(_PWD.sub("", inv.argv[0])):
        reason = "a program named by a variable or a command's output cannot be checked; name it plainly"
    elif inv.program in {"alias", "unalias"}:
        reason = "an alias hides what runs; run the command itself"
    elif inv.argv[0] in {"source", "."} and (
        len(inv.argv) < 2 or _expands(inv.argv[1]) or inv.argv[1].startswith(("/dev/", "/proc/"))
    ):
        reason = "source a file the guard can name"
    elif any(_could_be_a_flag(w) for w in _risky(inv)):
        reason = f"a {inv.program} word known only once the shell expands it cannot be checked; write it out"
    return Refusal("unreadable-command", reason) if reason else None


def _expands(word: str) -> bool:
    return "$" in word or "`" in word


def _could_be_a_flag(word: str) -> bool:
    """A word that becomes a flag once expanded (`$a`, `--no-${x}`); `HEAD~$n` or a message cannot."""
    return word.startswith(("$", "`")) or (word.startswith("-") and _expands(word))


def _risky(inv: shell.Invocation) -> list[str]:
    """The git, gh or factory words whose expansion could change what runs.

    A read-only git command, or `git add`, cannot skip a hook or move a branch, so only its flags count; gh's
    arguments past its command name a run or a pull request, except under `gh api`; factory's verb decides who may
    run it.
    """
    if inv.program in {"factory", "gh"}:
        words = _steering(inv.argv[1:])
        if inv.program == "gh" and words[:1] == ["api"]:
            return words
        return words[:2] + [w for w in words[2:] if w.startswith("-")]
    if inv.program != "git" and not inv.program.startswith("git-"):
        return []
    args = inv.argv[1:] if inv.program == "git" else [inv.program[4:], *inv.argv[1:]]
    sub, _ = git_rules.subcommand(args)
    words = _steering(args)
    if sub in _READ_ONLY:
        return [w for w in words if w.startswith("-")]
    return words


def _steering(args: list[str]) -> list[str]:
    """The words that steer a command: not a flag's value (`--author "$me"`, `--author="$me"`), nor a path after
    `--`."""
    words: list[str] = []
    rest = iter(args)
    for word in rest:
        if word == "--":
            break
        if word in _VALUED:
            next(rest, None)
        elif word.split("=", 1)[0] not in _VALUED or "=" not in word:
            words.append(word)
    return words
