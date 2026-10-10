"""The guard's rules for writing files: the factory's own files and the product's protected paths are the owner's
(`protected-paths`), the queue changes only through the factory command (`queue-status`), and an edit stays in the
session's own worktree (`own-worktree`).

A shell command is read for its redirects and for the files a program that changes files names; an edit tool for its
path. A product path counts from the top of the product's checkout or of any worktree of it, and as written.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from runtime.guard import shell
from runtime.guard.facts import Call, Decision, Refusal, inside, resolve
from runtime.shared.paths import MANAGED_SETTINGS
from runtime.shared.settings import Settings
from standard import config as product_config

# The paths only a person changes in every product: the goal and its pictures, git's own folder (config, hooks, refs
# and the pass record `factory check` keeps there), the surfaces that decide which testers a change needs, the
# workflows that reach staging and prod, the factory commit CI checks with, and factory.toml. Every script
# factory.toml names is protected too, and a product adds its own paths in repo.protected_paths.
ALWAYS_PROTECTED = (
    "GOAL.md",
    "goal/",
    ".git",
    "check-values/surfaces.toml",
    ".github/workflows/",
    ".github/factory-ref",
    product_config.FILE,
)
QUEUE = "queue"
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_MUTATORS = frozenset(
    {"cp", "mv", "rm", "rmdir", "ln", "chmod", "chown", "tee", "truncate", "install", "dd", "touch", "shred", "rsync"}
)
_REMOVERS = frozenset({"rm", "rmdir", "shred", "mv"})
# Programs that write only to their destination: what they copy from is read, not changed.
_COPIERS = frozenset({"cp", "install", "ln", "rsync"})
# A flag cluster that edits in place: sed's and perl's `-i`, alone or among others (`-pi`, `-ni`).
_IN_PLACE = re.compile(r"^-[a-zA-Z]*i")
_RECURSIVE = re.compile(r"^(-[a-zA-Z]*[rRa][a-zA-Z]*|--recursive|--archive)$")


def factory_roots(call: Call) -> list[Path]:
    """The factory's own files: its install, its state and sockets, the machine's Claude Code and factory config,
    and the agent user's own Claude Code and git configuration."""
    places = call.places
    return _with_real([
        places.install,
        places.state,
        places.run,
        Path("/etc") / places.install.name,
        MANAGED_SETTINGS.parent,
        call.home / ".claude",
        call.home / ".gitconfig",
        call.home / ".config" / "git",
    ])  # fmt: skip


def product_roots(call: Call) -> list[Path]:
    """The product's main checkout and every worktree the warden published, the session's own first."""
    roots = [call.worktree, Path(call.settings.get_str("repo.path"))]
    return _with_real(roots + [Path(s.worktree) for s in call.sessions.values() if s.worktree])


def _with_real(paths: list[Path]) -> list[Path]:
    """Each path as named and as it really is, so a link on the way to it hides nothing."""
    return list(dict.fromkeys(paths + [Path(os.path.realpath(p)) for p in paths]))


class UnknownProtectedPaths(Exception):
    """The main checkout's factory.toml cannot be read, so which scripts it protects is unknown."""


def protected_paths(settings: Settings) -> tuple[str, ...]:
    """Every protected path prefix of the product; a trailing / names a folder.

    The hooks come from the main checkout's factory.toml, which only a person changes: a worktree's copy could name
    fewer.
    """
    checkout = Path(settings.get_str("repo.path"))
    try:
        hooks = product_config.load(checkout).hooks()
    except product_config.ConfigError as exc:
        raise UnknownProtectedPaths(
            f"{checkout / product_config.FILE} cannot be read ({'; '.join(exc.problems)[:300]}), so the scripts it "
            "protects are unknown"
        ) from exc
    extras = tuple(str(p) for p in settings.get_list("repo.protected_paths") if str(p).strip())
    return ALWAYS_PROTECTED + hooks + extras


def is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether a repo-relative path is a protected prefix or inside one."""
    return any(_matches(PurePosixPath(path), prefix, removes=False) for prefix in prefixes)


def _matches(rel: PurePosixPath, fragment: str, removes: bool) -> bool:
    """Whether ``rel`` is the fragment or inside it, or, for a removal, holds it."""
    parts, frag = rel.parts, PurePosixPath(fragment).parts
    return parts[: len(frag)] == frag or (removes and frag[: len(parts)] == parts)


def _refuse_product(rel: PurePosixPath, prefixes: tuple[str, ...], removes: bool) -> Refusal | None:
    for fragment in prefixes:
        if _matches(rel, fragment, removes):
            return Refusal(
                "protected-paths",
                f"{fragment} is the owner's and only a person changes it; file a request with `factory request new`",
            )
    if _matches(rel, QUEUE, removes):
        return Refusal(
            "queue-status", "the queue is the warden's; change it only through the factory command (factory --help)"
        )
    return None


def protected(call: Call, word: str, cwd: Path, *, removes: bool = False) -> Refusal | None:
    """Whether writing (or, with ``removes``, removing) the path a word names is refused, and by which rule.

    While the protected paths are unknown every write is refused: the guard fails closed.
    """
    try:
        prefixes = protected_paths(call.settings)
    except UnknownProtectedPaths as exc:
        remedy = "every write is refused until the owner fixes it: file a request with `factory request new`"
        return Refusal("protected-paths", f"{exc}; {remedy}")
    candidates = [word] + ([word.split("=", 1)[1]] if "=" in word else [])
    for candidate in filter(None, candidates):
        path = resolve(candidate, cwd, call.home)
        for root in factory_roots(call):
            if inside(path, root) or (removes and inside(root, path)):
                return Refusal("protected-paths", f"{root} is the factory's own; only the owner changes it")
        for root in product_roots(call):
            if inside(path, root):
                found = _refuse_product(PurePosixPath(os.path.relpath(path, root)), prefixes, removes)
            elif removes and inside(root, path):
                found = Refusal("protected-paths", f"{root} is a checkout of the product; the warden removes those")
            else:
                continue
            if found is not None:
                return found
        written = PurePosixPath(os.path.normpath(candidate))
        if not candidate.startswith(("/", "~")) and written.parts[:1] != ("..",):
            found = _refuse_product(written, prefixes, removes=False)
            if found is not None:
                return found
    return None


def check_edit(call: Call) -> Decision:
    """An edit tool's path: never a protected path or the queue, and always inside the session's own worktree."""
    raw = str(call.tool_input.get("file_path") or call.tool_input.get("notebook_path") or "")
    if not raw:
        return None
    path = Path(os.path.realpath(resolve(raw, call.cwd, call.home)))
    found = protected(call, str(path), call.cwd)
    if found is not None and not call.off(found.rule):
        return found
    worktree = Path(os.path.realpath(call.worktree))
    # Other sessions' worktrees may sit inside the main checkout, which is the orchestrator's own.
    others = [Path(os.path.realpath(s.worktree)) for s in call.sessions.values() if s.worktree]
    nested = any(inside(path, other) and other != worktree and inside(other, worktree) for other in others)
    if (nested or not inside(path, worktree)) and not call.off("own-worktree"):
        return Refusal("own-worktree", f"edit only inside your own worktree, {worktree}")
    return None


def check_shell(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    """A shell command's writes: its redirects, and the files a program that changes files names."""
    found = _shell_writes(call, inv, cwd)
    return found if found is not None and not call.off(found.rule) else None


def _shell_writes(call: Call, inv: shell.Invocation, cwd: Path) -> Refusal | None:
    for target in inv.writes:
        if (found := protected(call, target, cwd)) is not None:
            return found
    args = inv.argv[1:]
    if inv.program in _COPIERS:
        target = _destination(args)
        # A tree copied into a folder that holds a protected path can overwrite it.
        tree = inv.program == "rsync" or any(_RECURSIVE.match(a) for a in args)
        return protected(call, target, cwd, removes=tree) if target else None
    in_place = inv.program in {"sed", "perl"} and any(_IN_PLACE.match(a) or a.startswith("--in-place") for a in args)
    if inv.program not in _MUTATORS and not in_place:
        return None
    # mv's last word is where things go; what it moves away is removed from where it was.
    gone = args[:-1] if inv.program == "mv" else args
    for word in (a for a in args if not a.startswith("-")):
        removes = inv.program in _REMOVERS and word in gone
        if (found := protected(call, word, cwd, removes=removes)) is not None:
            return found
    return None


def _destination(args: list[str]) -> str:
    """Where a copy goes: the `-t` folder, or the last word that is no flag."""
    for i, arg in enumerate(args):
        if arg in {"-t", "--target-directory"} and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--target-directory="):
            return arg.partition("=")[2]
    words = [a for a in args if not a.startswith("-")]
    return words[-1] if words else ""
