"""Install the product's toolchain: the programs its scripts need (install.toolchain), copied from the owner's PATH.

The factory's users never see the owner's home, so each program is copied root-owned and read-only into the
toolchain folder, and every unit that runs product code puts the folder's bin/ first on PATH. A program in a
``<prefix>/bin/`` beside a ``<prefix>/lib/`` (Node, with npm, npx and corepack) is copied with its whole prefix. Any
other is copied alone, with the files beside it whose names start with its name (uv's uvx). A program the owner's PATH
finds in a folder every unit's PATH already holds stays where it is. The copy replaces the last one whole.

The Pythons the product asks for (its .python-version, else its requires-python) come too: the owner's uv-managed
ones, each copied whole into the toolchain's python/, where every unit's uv looks and never downloads another.

A reinstall copies nothing when the plan and every file and folder it copies from are as they were at the last copy:
their size, time and inode, kept beside the copy, say so.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from runtime.shared.paths import FACTORY_BIN
from runtime.system import layout
from runtime.system.job import Install, refuse
from runtime.system.steps_prepare import login_path, owner_tool

# Each name goes into a shell command line, so it is a plain program name and nothing else.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# The name uv gives a managed Python's folder, such as cpython-3.14.4-linux-x86_64-gnu; it becomes a path here.
_PYTHON_KEY = re.compile(r"^[a-z]+-[0-9][A-Za-z0-9._+-]*$")
# The files a product pins a tool's version in, the first found counting; Python's pin is uv's (python_request).
_PINS = {"node": (".nvmrc", ".node-version")}
_VERSION = re.compile(r"^\d+(?:\.\d+){0,2}$")


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    prefix: str  # copied whole; "" when the program is copied alone
    files: tuple[tuple[str, str], ...]  # (source, program) copied alone; empty for a prefix
    programs: tuple[str, ...]  # linked into bin/: from the copy's bin/ for a prefix, from the copy itself otherwise


def _listing(ctx: Install, argv: list[str], folder: Path) -> list[str]:
    """The names of what ``argv`` lists in ``folder``, one path or name to a line."""
    code, out = ctx.host.query(argv, user=ctx.owner.name)
    if code != 0 and not ctx.host.dry:
        raise refuse(f"Your tools' folder {folder} cannot be read: check it is there, then install again")
    return sorted(Path(line.strip()).name for line in out.splitlines() if line.strip() and not line.endswith("/"))


def locate(ctx: Install, name: str) -> tuple[str, str] | None:
    """Where the owner's PATH finds ``name``, and the file that is with every link followed; None when it lacks it.
    A dry run that cannot look answers two empty paths."""
    host = ctx.host
    code, real = owner_tool(ctx, name)
    found = login_path(host, ctx.owner, name)[1]
    if code != 0 or (not real and not host.dry):
        return None
    return found, real


def tool(ctx: Install, name: str, found: str, real: str) -> Tool | None:
    """What to copy of ``name``; None when every unit's PATH finds it already."""
    host, owner = ctx.host, ctx.owner.name
    if not real:
        host.say(f"{name}: what {owner}'s PATH finds (a dry run does not look)")
        return Tool(name, "", ((f"<{name} on {owner}'s PATH>", name),), (name,))
    if {str(Path(found).parent), str(Path(real).parent)} & set(layout.SYSTEM_PATH):
        host.say(f"{name}: {found or real} is on every unit's PATH already; nothing to copy")
        return None
    bin_folder = Path(real).parent
    if bin_folder.name == "bin" and host.exists(bin_folder.parent / "lib"):
        programs = tuple(_listing(ctx, ["ls", "-1Ap", str(bin_folder)], bin_folder))
        host.say(f"{name}: the folder {bin_folder.parent}, with {', '.join(programs)}")
        return Tool(name, str(bin_folder.parent), (), programs)
    beside = Path(found or real).parent
    # Files only: a link beside it (another tool's launcher) belongs to something else.
    siblings = _listing(ctx, ["find", str(beside), "-maxdepth", "1", "-type", "f", "-name", f"{name}*"], beside)
    programs = tuple(sorted({name, *siblings}))
    host.say(f"{name}: {', '.join(programs)} from {beside}")
    return Tool(name, "", tuple((str(beside / p), p) for p in programs), programs)


def pin_problems(ctx: Install, real: dict[str, str]) -> list[str]:
    """Each tool whose version differs from the one the product pins (a version number in one of its ``_PINS``
    files): the agents and the stack would run another version than the product asks for."""
    problems = []
    for name, files in _PINS.items():
        pinned = next((ctx.product / f for f in files if (ctx.product / f).is_file()), None)
        if name not in real or not real[name] or pinned is None:
            continue
        lines = [line.strip() for line in pinned.read_text(encoding="utf-8").splitlines() if line.strip()]
        want = lines[0].removeprefix("v") if lines else ""
        if not _VERSION.match(want):
            ctx.host.say(f"{name}: {pinned.name} names {want!r}, not a version number, so it is not compared")
            continue
        code, out = ctx.host.query([real[name], "--version"], user=ctx.owner.name)
        have = out.strip().removeprefix("v")
        if code != 0 or not (have == want or have.startswith(want + ".")):
            problems.append(f"the product pins {name} {want} in {pinned.name}, and your shell's {name} is "
                            f"{have or 'unreadable'}")  # fmt: skip
    return problems


def python_request(product: Path) -> str:
    """The Python the product asks uv for: the first line of its .python-version, else its requires-python; "" when
    it names none."""
    pinned = product / ".python-version"
    if pinned.is_file():
        named = [line.strip() for line in pinned.read_text(encoding="utf-8").splitlines()]
        named = [line for line in named if line and not line.startswith("#")]
        if named:
            return named[0]
    project = product / "pyproject.toml"
    if not project.is_file():
        return ""
    try:
        declared = tomllib.loads(project.read_text(encoding="utf-8")).get("project", {}).get("requires-python", "")
    except tomllib.TOMLDecodeError as exc:
        raise refuse(
            f"The product's {project.name} cannot be read ({exc}): fix it in the product, then install again"
        ) from exc
    if not isinstance(declared, str):
        raise refuse(
            f"The product's {project.name} names its Python in a form uv does not read: fix it in the "
            "product, then install again"
        )
    return declared.strip()


def _json_line(out: str) -> object:
    """The one line of JSON uv printed; the query's output holds uv's warnings too."""
    line = next((each for each in out.splitlines() if each.startswith("[")), "")
    try:
        return json.loads(line)
    except ValueError as exc:
        raise refuse(
            "Your uv lists its Pythons in a form this install cannot read: update uv, then install again"
        ) from exc


def pythons(ctx: Install, *, fetched: bool = False) -> list[Path]:
    """The owner's uv-managed Pythons that answer the product's request, to copy; none when a Python on every unit's
    PATH answers it already. When the owner's uv has none, it fetches one, as the owner would by hand; refuses when
    the units could still run none."""
    host, owner, uv = ctx.host, ctx.owner.name, ctx.tool("uv")
    request = python_request(ctx.product)
    if not request:
        host.say("Python: the product asks for none")
        return []
    code, listed = host.query([uv, "python", "list", request, "--only-installed", "--output-format", "json"],
                              user=owner)  # fmt: skip
    if host.dry and not listed:
        host.say(f"Python {request}: what {owner}'s uv has installed (a dry run does not look)")
        return []
    dir_code, dir_out = host.query([uv, "python", "dir"], user=owner)
    managed = next((line.strip() for line in dir_out.splitlines() if line.startswith("/")), "")
    if code != 0 or dir_code != 0 or not managed:
        raise refuse("Your uv could not list its Pythons: update uv, then install again")
    found = _json_line(listed)
    entries = found if isinstance(found, list) else [found]
    copies: dict[str, Path] = {}
    reachable = []
    for entry in entries:
        key, path = (entry.get("key"), entry.get("path")) if isinstance(entry, dict) else (None, None)
        if not isinstance(key, str) or not _PYTHON_KEY.match(key) or not isinstance(path, str):
            host.say(f"uv listed a Python this install cannot read: {json.dumps(entry)[:200]}")
            raise refuse("Your uv lists its Pythons in a form this install cannot read: update uv, then install again")
        if Path(path).is_relative_to(managed):
            copies[key] = Path(managed) / key
        elif str(Path(path).parent) in layout.SYSTEM_PATH:
            reachable.append(path)
    if copies:
        host.say(f"Python {request}: {', '.join(sorted(copies))} from {managed}")
    elif reachable:
        host.say(f"Python {request}: {reachable[0]} is on every unit's PATH already; nothing to copy")
    elif not fetched and not host.dry:
        host.run([uv, "python", "install", request], user=owner)
        return pythons(ctx, fetched=True)
    else:
        raise refuse(f"The product asks for Python {request}, which your uv has not installed: run "
                     f"uv python install {shlex.quote(request)}, then install again")  # fmt: skip
    return [copies[key] for key in sorted(copies)]


def fingerprint(ctx: Install, tools: list[Tool], python_copies: list[Path]) -> str:
    """What the copy would be made from: the plan, and each source's size, time and inode. "" when a source cannot be
    looked at, so the copy is made again."""
    sources = [each.prefix for each in tools if each.prefix] + [src for each in tools for src, _ in each.files]
    # A program installed into a prefix (npm install -g) changes a folder inside it, never the prefix's own time.
    inside = ("bin", "lib", "lib/node_modules")
    sources += [f"{each.prefix}/{inner}" for each in tools if each.prefix for inner in inside
                if ctx.host.exists(Path(each.prefix) / inner)]  # fmt: skip
    sources += [str(path) for path in python_copies]
    code, out = ctx.host.query(["stat", "-L", "-c", "%n %s %Y %i", *sources], user=ctx.owner.name)
    if code != 0 or ctx.host.dry:
        return ""
    plan = {"tools": [asdict(each) for each in tools], "pythons": [str(p) for p in python_copies], "sources": out}
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


_FINGERPRINT = ".fingerprint"


def _place(ctx: Install, tools: list[Tool], python_copies: list[Path]) -> None:
    host, home = ctx.host, layout.TOOLCHAIN
    fresh, old = home.with_name(home.name + ".new"), home.with_name(home.name + ".old")
    for leftover in (fresh, old):
        if host.exists(leftover):
            host.remove(leftover)
    made = fingerprint(ctx, tools, python_copies)
    if made and host.exists(home / _FINGERPRINT) and host.read(home / _FINGERPRINT).strip() == made:
        host.say(f"the toolchain in {home} is as its sources are: kept")
        return
    host.mkdir(fresh / "bin", mode=0o755)
    python = fresh / layout.TOOLCHAIN_PYTHON.relative_to(home)
    host.mkdir(python, mode=0o755)
    for each_python in python_copies:
        # cp -a, not copy_tree, which leaves out __pycache__: the read-only copy can never write its bytecode again.
        host.run(["cp", "-a", str(each_python), str(python / each_python.name)])
    for each in tools:
        copy = fresh / each.name
        if each.prefix:
            host.copy_tree(Path(each.prefix), copy)
            links = {program: home / each.name / "bin" / program for program in each.programs}
        else:
            host.mkdir(copy, mode=0o755)
            for source, program in each.files:
                host.run(["install", "-m", "0755", "-o", "root", "-g", "root", source, str(copy / program)])
            links = {program: home / each.name / program for program in each.programs}
        for program, target in links.items():
            host.run(["ln", "-s", str(target), str(fresh / "bin" / program)])
    if made:
        host.write(fresh / _FINGERPRINT, made + "\n", mode=0o644)
    host.run(["chown", "-R", "-h", "root:root", str(fresh)])
    host.run(["chmod", "-R", "u=rwX,go=rX", str(fresh)])
    if host.exists(home):
        host.run(["mv", str(home), str(old)])
    host.run(["mv", str(fresh), str(home)])
    if host.exists(old):
        host.remove(old)


def toolchain(ctx: Install) -> None:
    names = ctx.need_settings().get_list("install.toolchain")
    bad = [str(name) for name in names if not isinstance(name, str) or not _NAME.match(name)]
    if bad:
        raise refuse(
            f"The setting install.toolchain names {', '.join(bad)}, which is not a program's name: fix the "
            "setting, then install again"
        )
    located = {name: locate(ctx, name) for name in names}
    missing = [name for name, where in located.items() if where is None]
    problems = [f"the product needs {', '.join(missing)}, which your shell does not find: install each for yourself, "
                "or take it out of the setting install.toolchain"] if missing else []  # fmt: skip
    problems += pin_problems(ctx, {name: where[1] for name, where in located.items() if where})
    if problems:
        raise refuse(f"The product's tools are not ready: {'; '.join(problems)}. Then install again")
    tools = []
    for name, where in located.items():
        planned = tool(ctx, name, *where) if where else None
        if planned is not None:
            tools.append(planned)
    owners: dict[str, str] = {}
    problems = []
    for each in tools:
        for program in each.programs:
            if program == FACTORY_BIN.name:
                problems.append(f"{each.name} brings a program named {program}, which would stand in for the factory's")
            elif program in owners:
                problems.append(f"{owners[program]} and {each.name} both bring a program named {program}")
            owners.setdefault(program, each.name)
    if problems:
        raise refuse(
            f"The product's tools cannot all be copied: {'; '.join(problems)}. Change the setting "
            "install.toolchain, then install again"
        )
    _place(ctx, tools, pythons(ctx))
