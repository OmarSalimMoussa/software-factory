"""Config readers: the environment is read only in config modules, and every key a service reads reaches it.

Two scans. First, product code reads the environment only in a config module (a file whose name is listed, or a file
of the same language inside a folder of that stem) or a listed shared reader. Any other read fails by line (strings
and comments masked; tests and vendored code out of scope) unless an exemption with its grounds answers it, and an
exemption that answers nothing is stale.
Second, for each listed service, every key its config module reads must be supplied by its compose service: through
an ``env_file`` (the keys of the tracked file listed for it), an ``environment`` entry, or an image ``ENV``. A
required read needs a value that always arrives: a literal or a ``${KEY:?}`` interpolation.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from checks.runner import mask, scope
from checks.runner.repo import Repo
from checks.standard.infra import configs

ID = "config-readers"
RULE = "The environment is read only in config modules, and every key a service reads reaches it"
VALUES_FILE = "config-readers.toml"
VALUES = {
    "config_files": [str],
    "readers": [str],
    "python_trees": [str],
    "script_trees": [str],
    "rust_trees": [str],
    "allow": {str: str},
    "accessors": {str: int},
    "optional": [str],
    "env_files": {str: str},
    "services": [{"compose": str, "name": str, "config": str}],
}

_SKIPPED_DIRS = frozenset({"vendor", "node_modules", "dist", "build", "testUtils", "testdata"})
_TEST_RE = re.compile(r"(_test\.py|conftest\.py|_testing\.py|_fakes\.py|(^|/)test_[^/]*\.py|\.(test|spec)\.[tj]sx?)$")
_PY_ENV_RE = re.compile(r"\b(os\.environ|os\.getenv|environ|getenv)\b")
_TS_ENV_RE = re.compile(r"\b(process\.env|import\.meta\.env|Deno\.env)\b")
_RS_ENV_RE = re.compile(r"\b(std::)?env::(var|var_os|vars)\s*\(")
_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=", re.M)
_TS_KEY_RE = re.compile(r"['\"]([A-Z][A-Z0-9_]{2,})['\"]")
_TS_COMMENT_LINE_RE = re.compile(r"^\s*(//|/\*|\*)")
_INTERPOLATION_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(:\?|:-|\?|-)?[^}]*\}")
_DOCKERFILE_ENV_RE = re.compile(r"^\s*ENV\s+([A-Za-z_][A-Za-z0-9_]*)=", re.M)


def _kind(rel: str, values: Mapping[str, Any]) -> str | None:
    if _TEST_RE.search(rel) or _SKIPPED_DIRS & set(rel.split("/")[:-1]):
        return None
    if rel.endswith(".py") and scope.under(rel, values["python_trees"]):
        return "py"
    if rel.endswith(scope.TS_SUFFIXES) and scope.under(rel, values["script_trees"]):
        return "ts"
    if rel.endswith(".rs") and scope.under(rel, values["rust_trees"]):
        return "rs"
    return None


def _env_lines(text: str, kind: str) -> list[int]:
    if kind == "py":
        return [n for n, line in mask.python_code(text).items() if _PY_ENV_RE.search(line)]
    pattern = _TS_ENV_RE if kind == "ts" else _RS_ENV_RE
    return [n for n, line in enumerate(mask.js_code(text), 1) if pattern.search(line)]


def _is_config(rel: str, names: list[str]) -> bool:
    path = PurePosixPath(rel)
    return any(path.name == name or (path.suffix == PurePosixPath(name).suffix and PurePosixPath(name).stem in
                                     path.parts[:-1]) for name in names)  # fmt: skip


def read_problems(repo: Repo, values: Mapping[str, Any]) -> list[str]:
    allow: dict[str, str] = values["allow"]
    config_names = values["config_files"]
    problems: list[str] = []
    answered: set[str] = set()
    for rel in repo.tracked:
        kind = _kind(rel, values)
        if kind is None or rel in values["readers"] or _is_config(rel, config_names) or repo.excluded(rel):
            continue
        try:
            hits = _env_lines(repo.read(rel), kind)
        except SyntaxError as exc:
            problems.append(f"{ID}: {rel} does not tokenize as Python: {exc}")
            continue
        if rel in allow:
            answered.update([rel] if hits else [])
            continue
        problems += [f"{ID}: {rel}:{n}: reads the environment outside a config module; read each key once, in the "
                     f"service's config module ({', '.join(config_names)})" for n in hits]  # fmt: skip
    problems += [f"{ID}: stale exemption: remove {rel} from allow" for rel in sorted(set(allow) - answered)]
    problems += [f"{ID}: listed reader {rel} is not a tracked file" for rel in values["readers"] if rel not in
                 repo.tracked]  # fmt: skip
    return problems


def config_reads(repo: Repo, rel: str, values: Mapping[str, Any]) -> dict[str, bool]:
    """The keys a config module reads, each mapped to whether the read is required."""
    text = repo.read(rel)
    if rel.endswith(scope.TS_SUFFIXES):
        code = "\n".join(line for line in text.splitlines() if not _TS_COMMENT_LINE_RE.match(line))
        return {key: True for key in _TS_KEY_RE.findall(code)}
    accessors: dict[str, int] = values["accessors"]
    keys: dict[str, bool] = {}
    for node in ast.walk(ast.parse(text)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in accessors):
            continue
        position = accessors[node.func.attr]
        argument = node.args[position] if len(node.args) > position else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            keys[argument.value] = node.func.attr not in values["optional"]
    return keys


def _always_arrives(value: object) -> bool:
    if value is None:
        return False
    return all(modifier in (":?", "?") for _key, modifier in _INTERPOLATION_RE.findall(str(value)))


def environment_supply(service: Mapping[str, Any]) -> dict[str, bool]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(key): _always_arrives(value) for key, value in environment.items()}
    supply: dict[str, bool] = {}
    for entry in environment if isinstance(environment, list) else []:
        key, separator, value = str(entry).partition("=")
        supply[key] = _always_arrives(value) if separator else False
    return supply


def _image_env(repo: Repo, compose: str, service: Mapping[str, Any]) -> set[str]:
    build = service.get("build")
    if build is None:
        return set()
    context = str(build.get("context", ".")) if isinstance(build, dict) else str(build)
    dockerfile = str(build.get("dockerfile", "Dockerfile")) if isinstance(build, dict) else "Dockerfile"
    path = (repo.root / compose).parent / context / dockerfile
    return set(_DOCKERFILE_ENV_RE.findall(path.read_text(encoding="utf-8"))) if path.is_file() else set()


def supply_of(repo: Repo, values: Mapping[str, Any], compose: str, service: Mapping[str, Any]) -> dict[str, bool]:
    """Each key the service receives, mapped to whether it always arrives with a value."""
    supply: dict[str, bool] = {}
    for env_file in configs.text_list(service.get("env_file")):
        listed = values["env_files"].get(env_file)
        if listed is None:
            raise configs.ConfigError(f"{compose}: env_file {env_file} is not in env_files: name the tracked file that "
                                      "lists its keys")  # fmt: skip
        if not (repo.root / listed).is_file():
            raise configs.ConfigError(f"{listed} (listed for env_file {env_file}) is missing")
        supply.update({key: True for key in _KEY_LINE_RE.findall(repo.read(listed))})
    supply.update({key: True for key in _image_env(repo, compose, service)})
    for key, required in environment_supply(service).items():
        supply[key] = required or supply.get(key, False)
    return supply


def service_problems(repo: Repo, values: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for row in values["services"]:
        label = f"{row['compose']}: service `{row['name']}`"
        try:
            entry = configs.services(repo, row["compose"]).get(row["name"])
            if entry is None:
                problems.append(f"{ID}: {row['compose']} has no service `{row['name']}`")
                continue
            if not (repo.root / row["config"]).is_file():
                problems.append(f"{ID}: {label}: its config module {row['config']} is missing")
                continue
            supply = supply_of(repo, values, row["compose"], entry)
            demands = config_reads(repo, row["config"], values)
        except (configs.ConfigError, SyntaxError) as exc:
            problems.append(f"{ID}: {label}: {exc}")
            continue
        for key, required in sorted(demands.items()):
            if key not in supply:
                problems.append(f"{ID}: {label} lacks {key} (read by {row['config']}): list it in its `environment` "
                                "or in a file its env_file loads")  # fmt: skip
            elif required and not supply[key]:
                problems.append(f"{ID}: {label} lacks {key} (read by {row['config']}): the read is required, so "
                                f"interpolate it as `${{{key}:?}}`")  # fmt: skip
    return problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return read_problems(repo, values) + service_problems(repo, values)
