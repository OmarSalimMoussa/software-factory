"""`git-bypass`: hooks are never skipped, refs are never rewritten, git's configuration is the owner's, the warden
pushes, the orchestrator merges with a merge commit, and no worktree or branch is removed by force. git's own
leading options are read past first, so `git -C <dir> push` is still a push."""

from __future__ import annotations

from runtime.guard import shell
from runtime.guard.facts import ORCHESTRATOR, Call, Decision, Refusal

# git's own options that come before the subcommand and take a value as the next word (`git -C <dir> log`).
GIT_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
READS = frozenset({"status", "log", "diff", "show", "branch", "rev-parse", "remote", "ls-files", "blame"})
# Commands that move a branch without a merge commit, so no merge hook runs; an implementer rebases its own branch.
_REWRITES = frozenset({"cherry-pick", "rebase", "pull", "am"})
# Configuration that runs a command or loads more configuration, given with `git -c`.
_RUNS_CODE = ("alias.", "include.", "includeif.", "core.hookspath", "core.fsmonitor", "core.sshcommand")
# git commit's flags whose value is the next word, which is never a flag of its own (`-m -n` is a message).
_COMMIT_VALUED = frozenset({"-m", "-F", "-C", "-c", "-t", "--message", "--file", "--template", "--author", "--date"})
_CONFIG_WRITES = frozenset(
    {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit"}
)
_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "get", "list"})
_BRANCH_MOVES = frozenset({"-f", "--force", "-M", "-m", "--move", "-C", "-c", "--copy", "-D", "-d", "--delete"})
_REMOTE_WRITES = frozenset({"add", "set-url", "rename", "remove", "rm", "set-branches", "set-head"})
_GITHUB_WRITES = frozenset(
    {
        ("pr", "merge"), ("repo", "edit"), ("repo", "delete"), ("repo", "rename"), ("repo", "archive"),
        ("release", "create"), ("release", "edit"), ("release", "delete"), ("secret", "set"), ("secret", "delete"),
        ("variable", "set"), ("variable", "delete"),
    }
)  # fmt: skip


def subcommand(args: list[str]) -> tuple[str, list[str]]:
    """The git subcommand past git's own leading options, and the words after it; empty when there is none."""
    i = 0
    while i < len(args):
        if args[i] in GIT_VALUED:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i], args[i + 1 :]
    return "", []


def git_args(inv: shell.Invocation) -> list[str] | None:
    """A git invocation's words after `git`; git's own programs run its subcommands by name (`git-send-pack`)."""
    if inv.program == "git":
        return inv.argv[1:]
    if inv.program.startswith("git-"):
        return [inv.program.removeprefix("git-"), *inv.argv[1:]]
    return None


def check(call: Call, inv: shell.Invocation) -> Decision:
    args = git_args(inv)
    if args is None or call.off("git-bypass"):
        return None
    sub, rest = subcommand(args)
    for rule in (_hooks, _push, _merge, _default_branch, _force):
        reason = rule(call, args, sub, rest)
        if reason:
            return Refusal("git-bypass", reason)
    return None


def _hooks(call: Call, args: list[str], sub: str, rest: list[str]) -> str:
    if any(_skips_hooks(arg) for arg in args) or (sub == "commit" and any(_short_n(a) for a in _commit_flags(rest))):
        return "git hooks are never skipped; fix what the hook found"
    if sub in {"update-ref", "filter-branch", "replace"}:
        return f"git {sub} rewrites refs behind the merge rules; commit and merge instead"
    if _sets_config(args, sub, rest) or (sub == "remote" and rest[:1] and rest[0] in _REMOTE_WRITES):
        return "git's configuration is the owner's; set nothing in it"
    return ""


def _push(call: Call, args: list[str], sub: str, rest: list[str]) -> str:
    if sub not in {"push", "send-pack", "http-push"}:
        return ""
    if call.role == ORCHESTRATOR and not call.in_subagent:
        return "the warden pushes: run `factory push <sha>` with the checked commit"
    return "only the orchestrator ships, through `factory push`; your branch stays local"


def _merge(call: Call, args: list[str], sub: str, rest: list[str]) -> str:
    if sub != "merge" or call.role != ORCHESTRATOR or {"--abort", "--continue", "--quit"} & set(rest):
        return ""
    return "" if "--no-ff" in rest else "merge with --no-ff, so each task lands as one merge commit"


def _default_branch(call: Call, args: list[str], sub: str, rest: list[str]) -> str:
    """The orchestrator's checkout is the default branch, which moves only by its --no-ff merge and a revert; its
    subagents share the checkout. A clone has no hooks."""
    if call.role != ORCHESTRATOR:
        return ""
    default = call.settings.get_str("repo.default_branch")
    if _moves_current(sub, rest) or _moves_named(sub, rest, default):
        return f"git {sub} moves {default} past the hooks; it moves only by a --no-ff merge and a revert"
    if sub == "clone":
        return "a clone has no hooks; work in the main checkout"
    return ""


def _moves_current(sub: str, rest: list[str]) -> bool:
    """A command that moves the checked-out branch with no merge commit, so no merge hook runs; aborting is fine."""
    if {"--abort", "--quit"} & set(rest):
        return False
    if sub in _REWRITES:
        return True
    if sub == "reset":
        # Any revision but HEAD moves the branch, even in the default mode; paths come after `--`.
        before = rest[: rest.index("--")] if "--" in rest else rest
        return any(r not in {"HEAD", "@"} for r in before if not r.startswith("-"))
    if sub in {"checkout", "switch"}:
        return bool({"-B", "-C", "--force-create"} & set(rest))
    return sub == "branch" and bool({"-M", "-m", "-f", "--force", "--move"} & set(rest))


def _moves_named(sub: str, rest: list[str], branch: str) -> bool:
    if sub == "fetch":
        if {"-u", "--update-head-ok"} & set(rest):
            return True
        dests = [a.rsplit(":", 1)[1] for a in rest if ":" in a and not a.startswith("-")]
        return any(d in {branch, f"refs/heads/{branch}"} for d in dests)
    if sub == "branch":
        return branch in rest and bool(_BRANCH_MOVES & set(rest))
    if sub in {"checkout", "switch"}:
        return branch in rest and bool({"-B", "-C", "--force-create"} & set(rest))
    return sub == "worktree" and rest[:1] == ["add"] and "-B" in rest


def _force(call: Call, args: list[str], sub: str, rest: list[str]) -> str:
    """git keeps a worktree with uncommitted files and a branch with unmerged commits; forcing it loses work."""
    short = {c for a in rest if a.startswith("-") and not a.startswith("--") for c in a[1:]}
    forced = _long(rest, "--force") or "f" in short
    if (sub == "worktree" and rest[:1] == ["remove"] and forced) or (
        sub == "branch" and ("D" in short or (forced and ("d" in short or _long(rest, "--delete"))))
    ):
        return f"a forced git {sub} deletes work git kept for a reason; leave it and file it for the owner"
    return ""


def _long(rest: list[str], option: str) -> bool:
    """The long option, or an abbreviation git accepts for it (`--forc`); an ambiguous one is refused all the same."""
    return any(len(a) > 2 and option.startswith(a) for a in rest)


def _skips_hooks(arg: str) -> bool:
    """`--no-verify`, or an abbreviation git accepts for it (`--no-veri`)."""
    return len(arg) >= len("--no-veri") and "--no-verify".startswith(arg)


def _commit_flags(rest: list[str]) -> list[str]:
    flags: list[str] = []
    words = iter(rest)
    for word in words:
        if word in _COMMIT_VALUED:
            next(words, None)
        else:
            flags.append(word)
    return flags


def _short_n(arg: str) -> bool:
    """A `git commit` flag cluster holding `-n` (no-verify) before any flag that takes a value (`-anm x`)."""
    if not arg.startswith("-") or arg.startswith("--"):
        return False
    for flag in arg[1:]:
        if flag == "n":
            return True
        if flag in "mFCctS":
            return False
    return False


def _sets_config(args: list[str], sub: str, rest: list[str]) -> bool:
    for i, arg in enumerate(args):
        value = args[i + 1] if arg == "-c" and i + 1 < len(args) else ""
        if arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            value = arg[2:]  # `-ccore.hooksPath=…`, written attached
        if value.lower().startswith(_RUNS_CODE):
            return True
        # --exec-path names where git finds its own programs.
        if arg.startswith(("--config-env", "--exec-path")):
            return True
    if sub != "config":
        return False
    if _CONFIG_WRITES & set(rest) or rest[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"], ["edit"]):
        return True
    positional = [a for a in rest if not a.startswith("-")]
    return len(positional) >= 2 and not (_CONFIG_READS & set(rest))


def sets_config_from_env(inv: shell.Invocation) -> bool:
    """git configuration given through the environment, which every git command after it reads."""
    names = list(inv.assigned)
    if inv.program in {"export", "declare", "typeset", "readonly", "local"}:
        names += [a.split("=", 1)[0] for a in inv.argv[1:] if not a.startswith("-")]
    return any(name.startswith("GIT_CONFIG") for name in names)


def github_writes(call: Call, inv: shell.Invocation) -> Decision:
    """GitHub changes only through the warden: an API write, a merge, or a repo, release, secret or variable change
    goes round it."""
    if inv.program != "gh" or call.off("git-bypass"):
        return None
    args = inv.argv[1:]
    if (args[:1] == ["api"] and api_writes(args)) or tuple(args[:2]) in _GITHUB_WRITES:
        return Refusal("git-bypass", "GitHub changes only through the warden; ask the orchestrator")
    return None


def api_writes(args: list[str]) -> bool:
    """Whether a `gh api` call sends anything but a GET."""
    if any(a in {"-f", "-F", "--field", "--raw-field", "--input"} for a in args):
        return True
    for i, arg in enumerate(args):
        if arg in {"-X", "--method"} and i + 1 < len(args) and args[i + 1].upper() != "GET":
            return True
        if arg.startswith(("-X", "--method=")) and len(arg) > 2 and not arg.upper().endswith("GET"):
            return True
    return False
