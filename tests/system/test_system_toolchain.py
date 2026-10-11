"""The product's toolchain: copied from the owner's PATH, root-owned, and linked first on every product unit's PATH."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from system_fakes import dry_job, lookup

from runtime.shared.protocol import Refused
from runtime.system import install
from runtime.system.host import DryHost
from runtime.system.install_steps import steps_prepare, steps_toolchain
from runtime.system.job import Install

NEW, LIVE = "/opt/software-factory-toolchain.new", "/opt/software-factory-toolchain"
KEY = "cpython-3.14.4-linux-x86_64-gnu"


def _on_path(job: Install, name: str, found: str, real: str = "") -> None:
    assert isinstance(job.host, DryHost)
    # The shell's start-up files print before the lookup answers.
    job.host.answers[lookup(name)] = (0, f"Now using node v22\n{steps_prepare.LOOKUP_MARK}\n{found}\n")
    job.host.answers[f"readlink -f {found}"] = (0, real or found)


def _listed(job: Install, folder: str, *names: str) -> None:
    assert isinstance(job.host, DryHost)
    job.host.answers[f"ls -1Ap {folder}"] = (0, "".join(f"{name}\n" for name in names))


def _files(job: Install, folder: str, name: str, *found: str) -> None:
    assert isinstance(job.host, DryHost)
    listed = "".join(f"{folder}/{each}\n" for each in found)
    job.host.answers[f"find {folder} -maxdepth 1 -type f -name {name}*"] = (0, listed)


def _owners_tools(job: Install, node: str) -> str:
    """uv in ~/.local/bin, beside uvx; node where the case puts it."""
    assert isinstance(job.host, DryHost)
    home = str(job.owner.home)
    _on_path(job, "uv", f"{home}/.local/bin/uv")
    _files(job, f"{home}/.local/bin", "uv", "uv", "uvx")
    if node == "nvm":
        prefix = f"{home}/.nvm/versions/node/v22.1.0"
        _on_path(job, "node", f"{prefix}/bin/node")
        _listed(job, f"{prefix}/bin", "corepack", "node", "npm", "npx")
        job.host.paths.add(Path(prefix) / "lib")
    else:
        _on_path(job, "node", "/usr/local/bin/node")
        job.host.paths.add(Path("/usr/local/lib"))
    return home


@pytest.mark.parametrize(
    ("node", "copied"),
    [
        ("nvm", [
            "copy: {home}/.nvm/versions/node/v22.1.0 to {new}/node",
            *(f"run: ln -s {{live}}/node/bin/{p} {{new}}/bin/{p}" for p in ("corepack", "node", "npm", "npx")),
        ]),
        # Found where every unit's PATH looks already: the system's /usr/local is never copied.
        ("system", []),
    ],
    ids=["a prefix", "on every unit's path"],
)  # fmt: skip
def test_the_dry_run_lists_each_copy_and_link(tmp_path: Path, node: str, copied: list[str]) -> None:
    job = dry_job(tmp_path)
    home = _owners_tools(job, node)

    install.run(job)

    assert isinstance(job.host, DryHost)
    done = [action for action in job.host.actions if "software-factory-toolchain" in action]
    uv = [
        f"make: {NEW}/uv/ (755, root:root)",
        f"run: install -m 0755 -o root -g root {home}/.local/bin/uv {NEW}/uv/uv",
        f"run: install -m 0755 -o root -g root {home}/.local/bin/uvx {NEW}/uv/uvx",
        f"run: ln -s {LIVE}/uv/uv {NEW}/bin/uv",
        f"run: ln -s {LIVE}/uv/uvx {NEW}/bin/uvx",
    ]
    assert done == [
        f"make: {NEW}/bin/ (755, root:root)",
        f"make: {NEW}/python/ (755, root:root)",
        *uv,
        *(line.format(home=home, new=NEW, live=LIVE) for line in copied),
        f"run: chown -R -h root:root {NEW}",
        f"run: chmod -R u=rwX,go=rX {NEW}",
        f"run: mv {NEW} {LIVE}",
    ]


def _pythons(job: Install, request: str, *found: tuple[str, str]) -> None:
    """The owner's uv lists ``found`` (key, path) as the installed Pythons that answer ``request``."""
    assert isinstance(job.host, DryHost)
    uv = f"{job.owner.home}/.local/bin/uv"
    listed = [{"key": key, "version": "3", "path": path, "symlink": None} for key, path in found]
    job.host.answers[f"{uv} python list {request} --only-installed --output-format json"] = (
        0, "warning: a newer uv is out\n" + json.dumps(listed) + "\n"
    )  # fmt: skip
    job.host.answers[f"{uv} python dir"] = (0, f"{job.owner.home}/.local/share/uv/python\n")


@pytest.mark.parametrize(
    ("asks", "asked", "python", "copied"),
    [
        # uv lists it through its minor version's link; the folder named by its key is what is copied.
        ((".python-version", "# the product's Python\n3.14\n"), "3.14",
         "{uv}/cpython-3.14-linux-x86_64-gnu/bin/python3.14", True),
        # The system's own Python answers it, and every unit finds that already.
        (("pyproject.toml", '[project]\nrequires-python = ">=3.12"\n'), ">=3.12", "/usr/bin/python3.12", False),
    ],
    ids=["a managed python", "the system's python"],
)  # fmt: skip
def test_the_python_the_product_asks_for_is_copied_from_the_owners_uv(
    tmp_path: Path, asks: tuple[str, str], asked: str, python: str, copied: bool
) -> None:
    job = dry_job(tmp_path)
    _owners_tools(job, "system")
    (job.product / asks[0]).write_text(asks[1])
    uv = f"{job.owner.home}/.local/share/uv/python"
    _pythons(job, asked, (KEY, python.format(uv=uv)))

    install.run(job)

    assert isinstance(job.host, DryHost)
    done = [action for action in job.host.actions if f"{NEW}/python" in action]
    assert done == [f"make: {NEW}/python/ (755, root:root)", *([f"run: cp -a {uv}/{KEY} {NEW}/python/{KEY}"] * copied)]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing", "the product needs node, which your shell does not find"),
        ("pinned", "the product pins node 20 in .nvmrc, and your shell's node is 22.1.0"),
        ("shadow", "node brings a program named factory, which would stand in for the factory's"),
        ("python", "Python 3.14, which your uv has not installed: run uv python install 3.14, then install again"),
    ],
)
def test_a_tool_the_owner_lacks_or_one_that_shadows_the_factory_refuses_the_install(
    tmp_path: Path, case: str, reason: str
) -> None:
    job = dry_job(tmp_path)
    home = _owners_tools(job, "nvm")
    assert isinstance(job.host, DryHost)
    if case == "missing":
        job.host.answers[lookup("node")] = (1, steps_prepare.LOOKUP_MARK + "\n")
    elif case == "pinned":
        (job.product / ".nvmrc").write_text("v20\n")
        job.host.answers[f"{home}/.nvm/versions/node/v22.1.0/bin/node --version"] = (0, "v22.1.0\n")
    elif case == "shadow":
        _listed(job, f"{home}/.nvm/versions/node/v22.1.0/bin", "factory", "node")
    else:
        (job.product / ".python-version").write_text("3.14\n")
        _pythons(job, "3.14", (KEY, "/home/owner/.pyenv/versions/3.14.4/bin/python3.14"))

    with pytest.raises(Refused, match=reason):
        install.run(job)


def test_a_python_the_owners_uv_lacks_is_fetched_with_it_once_before_the_install_refuses(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    _owners_tools(job, "nvm")
    assert isinstance(job.host, DryHost)
    job.host.dry = False  # decides like a real install; a dry host still changes nothing
    job.tools["uv"] = uv = f"{job.owner.home}/.local/bin/uv"
    (job.product / ".python-version").write_text("3.14\n")
    _pythons(job, "3.14")

    with pytest.raises(Refused, match="which your uv has not installed"):
        steps_toolchain.pythons(job)

    assert [a for a in job.host.actions if "python install" in a] == [f"run: {uv} python install 3.14 (as owner)"]
