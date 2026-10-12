"""`own-branch`: linked worktrees share one `.git`, so a session moves only its own branch.

A session's own branch is the one its worktree has checked out (the orchestrator's is the default branch). A git
command that names another ref to create, move or delete is refused; so is one that would move the checked-out
branch of a directory that is on another branch, and one that leaves the own branch for another. Worktrees are the
warden's: it adds them and reaps them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from runtime.guard import deadline, shell
from runtime.guard.facts import ORCHESTRATOR, Call, Decision, Refusal, resolve
from runtime.guard.git_rules import GIT_VALUED, git_args, subcommand

# Commands that move the checked-out branch of the directory they run in.
_MOVERS = frozenset({"commit", "merge", "rebase", "cherry-pick", "revert", "am", "pull", "reset"})
_ENDS = frozenset({"--abort", "--quit", "--skip"})
# Flags whose value is the next word, per command, so a value is never read as a branch.
_VALUED = {
    "branch": frozenset({"--set-upstream-to", "-u", "--contains", "--no-contains", "--merged", "--no-merged",
                         "--points-at", "--sort", "--format", "--track"}),
    "checkout": frozenset({"-b", "-B", "--orphan", "--conflict", "--pathspec-from-file"}),
    "switch": frozenset({"-c", "-C", "--orphan", "--conflict"}),
    "rebase": frozenset({"--onto", "-x", "--exec", "-s", "--strategy", "-X", "--strategy-option"}),
    "tag": frozenset({"-m", "-F", "--message", "--file", "--contains", "--no-contains", "--points-at", "--sort",
                      "--format", "-u", "--local-user", "--merged", "--no-merged"}),
    "update-ref": frozenset({"-m"}),
}  # fmt: skip
_BRANCH_READS = frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes", "--show-current", "-v", "-vv"})
_TAG_READS = frozenset({"-l", "--list", "-v", "--verify", "-n"})
_CREATES = {"checkout": ("-b", "-B", "--orphan"), "switch": ("-c", "-C", "--orphan")}


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True,
                              timeout=deadline.left(), check=False)  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise deadline.OutOfTime(f"git {args[0]} did not answer in the guard's time") from exc


def current_branch(directory: Path) -> str | None:
    """The branch checked out in a directory; None when it is detached or no checkout."""
    done = _git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    return done.stdout.strip() or None if done.returncode == 0 else None


def head(directory: Path) -> str | None:
    """The commit a checkout has checked out; None when it has none."""
    done = _git(directory, "rev-parse", "--verify", "--quiet", "HEAD")
    return done.stdout.strip() or None if done.returncode == 0 else None


def _is_commit(directory: Path, word: str) -> bool:
    return _git(directory, "rev-parse", "--verify", "--quiet", f"{word}^{{commit}}").returncode == 0


def own_branch(call: Call) -> str | None:
    if call.role == ORCHESTRATOR:
        return call.settings.get_str("repo.default_branch")
    return current_branch(call.worktree)


def _positionals(sub: str, rest: list[str]) -> list[str]:
    valued = _VALUED.get(sub, frozenset())
    words: list[str] = []
    it = iter(rest)
    for word in it:
        if word == "--":
            break
        if word in valued:
            next(it, None)
        elif not word.startswith("-"):
            words.append(word)
    return words


def _short_ref(ref: str) -> str:
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") or "/" not in ref else ref


def named_refs(sub: str, rest: list[str]) -> list[str]:
    """The refs other than HEAD that a git command creates, moves or deletes; `tag:` marks a tag."""
    if sub in _CREATES:
        return [rest[i + 1] for i, a in enumerate(rest[:-1]) if a in _CREATES[sub]]
    named = _NAMED.get(sub)
    return named(_positionals(sub, rest), set(rest)) if named else []


def _branch_refs(words: list[str], flags: set[str]) -> list[str]:
    if not words or _BRANCH_READS & flags or any(f.startswith("--format") for f in flags):
        return []
    return words if {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy"} & flags else words[:1]


def _tag_refs(words: list[str], flags: set[str]) -> list[str]:
    if not words or _TAG_READS & flags:
        return []
    return [f"tag:{w}" for w in (words if {"-d", "--delete"} & flags else words[:1])]


# Per git command other than checkout and switch: the refs it names, from its positional words and its flags.
_NAMED: dict[str, Callable[[list[str], set[str]], list[str]]] = {
    "update-ref": lambda words, _: [_short_ref(words[0])] if words else ["(unnamed)"],
    "branch": _branch_refs,
    "fetch": lambda words, _: [
        _short_ref(a.lstrip("+").rsplit(":", 1)[1]) for a in words if ":" in a and a.rsplit(":", 1)[1]
    ],
    "rebase": lambda words, _: words[1:2],
    "tag": _tag_refs,
    "symbolic-ref": lambda words, _: [_short_ref(w) for w in words[1:2]],
    "stash": lambda words, _: words[1:2] if words[:1] == ["branch"] else [],
}


def _switches_to(directory: Path, sub: str, rest: list[str]) -> str | None:
    """Where a checkout or switch would move HEAD; None when it only restores files."""
    if sub not in _CREATES or "--" in rest or any(a in _CREATES[sub] for a in rest):
        return None
    if "--detach" in rest or "-" in rest:
        return "(another commit)"
    words = _positionals(sub, rest)
    if sub == "switch":
        return words[0] if words else None
    return words[0] if len(words) == 1 and _is_commit(directory, words[0]) else None


def _directory(args: list[str], cwd: Path, home: Path) -> Path | None:
    """The checkout a git command acts on: its `-C` folders from where it runs; None when it names another way."""
    directory, i = cwd, 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in {"--git-dir", "--work-tree"} or arg.startswith(("--git-dir=", "--work-tree=")):
            return None
        if arg == "-C" and i + 1 < len(args):
            directory = resolve(args[i + 1], directory, home)
        i += 2 if arg in GIT_VALUED else 1
    return directory


def check(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    args = git_args(inv)
    if args is None or call.off("own-branch"):
        return None
    sub, rest = subcommand(args)
    if sub == "worktree" and rest[:1] not in ([], ["list"]):
        return Refusal("own-branch", "the warden adds and removes worktrees; work in your own")
    directory = _directory(args, cwd, call.home)
    refs = named_refs(sub, rest)
    moves = sub in _MOVERS and not (_ENDS & set(rest))
    if directory is None:
        if refs or moves or sub in _CREATES:
            return Refusal("own-branch", "name your checkout with -C or cd, never --git-dir or --work-tree")
        return None
    return _off_own_branch(call, sub, directory, refs, moves, _switches_to(directory, sub, rest))


def _off_own_branch(
    call: Call, sub: str, directory: Path, refs: list[str], moves: bool, switch: str | None
) -> Decision:
    """The refusal when git SUB in DIRECTORY would change a branch other than the caller's own."""
    if not (refs or moves or switch):
        return None
    own = own_branch(call)
    other = [r for r in refs if r != own]
    if other:
        mine = f"your own branch, {own}" if own else "your own branch, which is not checked out now"
        return Refusal("own-branch", f"git {sub} would change {', '.join(other)}; change only {mine}")
    if switch and switch != own:
        return Refusal("own-branch", f"stay on your own branch, {own}; restore files with `git checkout -- <path>`")
    current = current_branch(directory) if moves else None
    if current is not None and current != own:
        return Refusal("own-branch", f"git {sub} here would move {current}; work in your own worktree, on {own}")
    return None
