"""The build plan: which images CI builds for a commit, in what order, and which files a deploy needs.

Every compose service with a build section is an image named after the service, and so is each of
``[build].extra_images``. An image whose Dockerfile builds FROM another product image tagged ``${tag_arg}`` derives
from it and builds after it; that base must not derive from a third (one level only). CI has no product env, so a
build arg may name only ``commit_arg`` or ``tag_arg``, which become the short commit; ``commit_arg`` is always passed,
and ``tag_arg`` too for a derived image. The files are ``[deploy].files``, every after-deploy hook and factory.toml.

CI runs ``PYTHONPATH=<factory> python3 -m standard.plan --repo . --commit <sha> --factory-commit <sha> --out
plan.json [--github-output]``; the last appends ``base``, ``derived`` (compact JSON lists of images), ``short`` and
``has_derived`` to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from standard import compose as compose_mod
from standard import config as config_mod
from standard.compose import Compose, ComposeError
from standard.config import Config, ConfigError

DEPLOY_IMAGE = "deploy"
SHORT = 8
_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
# $$ (a literal $), ${NAME...} with any modifier, or $NAME: the references compose and Docker substitute.
_REFERENCE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class PlanError(Exception):
    """The product cannot be planned; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Image:
    name: str
    service: str | None  # the compose service it is built for; None for an extra image
    context: str  # repo-relative
    dockerfile: str  # repo-relative
    target: str | None
    args: dict[str, str]
    after: str | None  # the image it derives from, which builds first
    ref: str  # <registry>/<product>-<name>, without a tag


@dataclass(frozen=True, slots=True)
class Plan:
    product: str
    registry: str
    commit: str
    short: str
    factory_commit: str
    images: tuple[Image, ...]  # every base image, then every derived one
    files: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class _Source:
    name: str
    service: str | None
    where: str
    context: str
    dockerfile: str
    target: str | None
    args: tuple[tuple[str, str | None], ...]
    bases: list[str] = dataclasses.field(default_factory=list)


def files(config: Config) -> tuple[str, ...]:
    hooks = [hook for ws in config.workspaces for hook in ws.after_deploy]
    return tuple(dict.fromkeys([*config.deploy.files, *hooks, config_mod.FILE]))


def images(repo: Path, config: Config, compose: Compose, short: str) -> tuple[Image, ...]:
    """Every image CI builds, base images first; PlanError lists every problem."""
    problems: list[str] = []
    sources = _sources(config, compose, problems)
    known = {s.name for s in sources}
    for source in sources:
        source.bases = _bases(repo, config, source, problems)
        for base in source.bases:
            if base not in known:
                problems.append(f"{source.dockerfile}: FROM names {image_ref(config, base)}, which no compose "
                                "service and no [build].extra_images entry builds")  # fmt: skip
    by_name = {s.name: s for s in sources}
    built: list[Image] = []
    for source in sources:
        if len(source.bases) > 1:
            problems.append(f"{source.dockerfile}: builds FROM {', '.join(source.bases)}; an image derives from one "
                            "product image at most")  # fmt: skip
        after = source.bases[0] if source.bases else None
        if after == source.name:
            problems.append(f"{source.dockerfile}: {source.name} builds FROM its own image; name a base image")
        elif after in by_name and by_name[after].bases:
            problems.append(f"{source.dockerfile}: {source.name} derives from {after}, which itself derives from "
                            f"{by_name[after].bases[0]}; derive from a base image only (one level)")  # fmt: skip
        args = _args(config, source, short, problems)
        args[config.build.commit_arg] = short
        if after is not None:
            args[config.build.tag_arg] = short
        built.append(Image(source.name, source.service, source.context, source.dockerfile, source.target, args, after,
                           image_ref(config, source.name)))  # fmt: skip
    if not built:
        problems.append(f"{compose_mod.FILE} builds no image and [build].extra_images is empty: a deploy needs one")
    if problems:
        raise PlanError(problems)
    return tuple([i for i in built if i.after is None] + [i for i in built if i.after is not None])


def make(repo: Path, commit: str, factory_commit: str) -> Plan:
    shas = (("--commit", commit), ("--factory-commit", factory_commit))
    problems = [f"{what} must be a full 40-character lowercase sha, not {sha!r}" for what, sha in shas
                if not _SHA.match(sha)]  # fmt: skip
    if problems:
        raise PlanError(problems)
    try:
        config = config_mod.load(repo)
    except ConfigError as exc:
        raise PlanError([f"{config_mod.FILE}: {p}" for p in exc.problems]) from exc
    try:
        compose = compose_mod.read(repo / compose_mod.FILE)
    except ComposeError as exc:
        raise PlanError([f"{compose_mod.FILE}: {p}" for p in exc.problems]) from exc
    short = commit[:SHORT]
    found = images(repo, config, compose, short)
    return Plan(config.product.name, config.product.registry, commit, short, factory_commit, found, files(config))


def read(data: object) -> Plan:
    """A plan.json as this factory writes it, or PlanError: its fields must be exactly a Plan's, and each image's
    exactly an Image's. The server deploys any build whose plan has this shape, whichever factory commit planned it,
    so pushing prod back to a build an older factory planned works for as long as the shape holds."""
    want, want_image = [f.name for f in dataclasses.fields(Plan)], [f.name for f in dataclasses.fields(Image)]
    if not isinstance(data, dict) or sorted(data) != sorted(want):
        found = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise PlanError([f"want exactly the fields {want}, not {found}"])
    images, problems = data["images"], []
    if not isinstance(images, list) or not images:
        raise PlanError(["images is not a list of images"])
    for n, image in enumerate(images):
        if not isinstance(image, dict) or sorted(image) != sorted(want_image):
            problems.append(f"image {n + 1} wants exactly the fields {want_image}")
        elif not isinstance(image["ref"], str) or not isinstance(image["name"], str):
            problems.append(f"image {n + 1}'s name and ref are not text")
    files = data["files"]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        problems.append("files is not a list of paths")
    if problems:
        raise PlanError(problems)
    return Plan(**{**data, "images": tuple(Image(**i) for i in images), "files": tuple(files)})


def image_ref(config: Config, image: str) -> str:
    """``<registry>/<product>-<image>``, without a tag."""
    return f"{config.product.registry}/{config.product.name}-{image}"


def reference(names: Sequence[str]) -> str:
    """A pattern for one reference to any of ``names``: ``$NAME``, ``${NAME}``, or ``${NAME:-x}`` and its kin."""
    alternatives = "|".join(map(re.escape, names))
    return rf"\$(?:\{{(?:{alternatives})(?::?[-?][^}}]*)?\}}|(?:{alternatives})(?![A-Za-z0-9_]))"


def _sources(config: Config, compose: Compose, problems: list[str]) -> list[_Source]:
    found: list[_Source] = []
    for service in compose.built():
        build = service.build
        assert build is not None
        where = f"{compose_mod.FILE}: service {service.name}"
        context = _repo_path(where, "build.context", build.context, problems)
        dockerfile = _repo_path(where, "build.dockerfile", os.path.join(build.context, build.dockerfile), problems)
        if build.target is not None and "$" in build.target:
            problems.append(f"{where}: build.target {build.target!r} names a variable CI cannot fill; write it out")
        found.append(_Source(service.name, service.name, where, context, dockerfile, build.target, build.args))
    for extra in config.build.extra_images:
        where = f"factory.toml: [build].extra_images {extra.name}"
        context = _repo_path(where, "context", extra.context, problems)
        found.append(_Source(extra.name, None, where, context, _repo_path(where, "dockerfile", extra.dockerfile,
                                                                            problems), None, ()))  # fmt: skip
    seen: set[str] = set()
    for source in found:
        if not _IMAGE.fullmatch(source.name):
            problems.append(f"{source.where}: {source.name!r} cannot name an image; use lowercase letters, digits, -")
        if source.name == DEPLOY_IMAGE:
            problems.append(f"{source.where}: '{DEPLOY_IMAGE}' is the factory's own image; rename the service")
        if source.name in seen:
            problems.append(f"{source.where}: the image {source.name} is built twice; rename one of them")
        seen.add(source.name)
    return found


def _repo_path(where: str, key: str, value: str, problems: list[str]) -> str:
    if "$" in value:
        problems.append(f"{where}: {key} {value!r} names a variable CI cannot fill; write the path out")
        return value
    path = os.path.normpath(value)
    if os.path.isabs(path) or path == ".." or path.startswith("../") or "://" in value:
        problems.append(f"{where}: {key} {value!r} must be a path inside the repo")
    return path


def _bases(repo: Path, config: Config, source: _Source, problems: list[str]) -> list[str]:
    """The product images the Dockerfile builds FROM, by image name."""
    try:
        parsed = compose_mod.dockerfile(repo / source.dockerfile)
    except ComposeError as exc:
        problems += [f"{source.where}: {source.dockerfile} {p}" for p in exc.problems]
        return []
    values = {**parsed.args, **{k: v for k, v in source.args if v is not None and "$" not in v}}
    prefix = f"{config.product.registry}/{config.product.name}-"
    tag = re.compile(reference([config.build.tag_arg]))
    found: list[str] = []
    for line in parsed.froms:
        image = _expand(line, values, keep=config.build.tag_arg)
        if not image.startswith(prefix):
            continue
        name, colon, suffix = image[len(prefix) :].partition(":")
        if "@" in name or not colon or not tag.fullmatch(suffix):
            problems.append(f"{source.dockerfile}: FROM {line} names a product image, so its tag must be "
                            f"${{{config.build.tag_arg}}}: the image built for the same commit")  # fmt: skip
            continue
        found.append(name)
    return list(dict.fromkeys(found))


def _expand(text: str, values: Mapping[str, str | None], keep: str) -> str:
    """``text`` with each variable that has a value (or a ``:-`` default) filled in; ``keep`` and unknown variables
    stay as written."""

    def one(match: re.Match[str]) -> str:
        name, modifier = match.group(1) or match.group(3), match.group(2) or ""
        if name is None or name == keep:
            return match.group(0)
        value = values.get(name)
        if modifier.startswith((":-", "-")):
            unset = value is None or (modifier.startswith(":") and value == "")
            return modifier.split("-", 1)[1] if unset else str(value)
        return match.group(0) if value is None else value

    return _REFERENCE.sub(one, text)


def _args(config: Config, source: _Source, short: str, problems: list[str]) -> dict[str, str]:
    """The build args with the commit filled in; any other variable is a problem, as CI has no product env."""
    own = (config.build.commit_arg, config.build.tag_arg)
    found: dict[str, str] = {}
    for key, value in source.args:
        if value is None:
            if key in own:
                found[key] = short
            else:
                problems.append(f"{source.where}: build arg {key} takes its value from the environment, which CI "
                                "does not have; give it a literal value")  # fmt: skip
            continue
        strangers = [m.group(1) or m.group(3) for m in _REFERENCE.finditer(value) if m.group(0) != "$$"]
        unknown = [name for name in strangers if name not in own]
        if unknown:
            problems.append(f"{source.where}: build arg {key} names ${{{unknown[0]}}}, which CI cannot fill (it "
                            f"has no product env); write a literal value, or name ${{{own[0]}}} or "
                            f"${{{own[1]}}}")  # fmt: skip
            continue
        found[key] = _REFERENCE.sub(lambda m: "$" if m.group(0) == "$$" else short, value)
    return found


def _github_output(plan: Plan) -> str:
    def compact(items: list[Image]) -> str:
        return json.dumps([dataclasses.asdict(i) for i in items], separators=(",", ":"))

    base = [i for i in plan.images if i.after is None]
    derived = [i for i in plan.images if i.after is not None]
    return (f"base={compact(base)}\nderived={compact(derived)}\nshort={plan.short}\n"
            f"has_derived={'true' if derived else 'false'}\n")  # fmt: skip


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.plan", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--factory-commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-output", action="store_true", help="append the outputs to $GITHUB_OUTPUT")
    args = parser.parse_args(argv)
    output = os.environ.get("GITHUB_OUTPUT") if args.github_output else None
    if args.github_output and not output:
        print("plan: --github-output needs GITHUB_OUTPUT, the file GitHub Actions reads outputs from", file=sys.stderr)
        return 2
    try:
        plan = make(args.repo, args.commit, args.factory_commit)
    except PlanError as exc:
        print("plan: the product cannot be built:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(plan.as_json(), indent=2) + "\n", encoding="utf-8")
    if output:
        with open(output, "a", encoding="utf-8") as sink:
            sink.write(_github_output(plan))
    for image in plan.images:
        print(f"{image.ref}:{plan.short}" + (f" after {image.after}" if image.after else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
