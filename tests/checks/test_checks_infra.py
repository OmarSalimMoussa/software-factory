from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from checks.runner.repo import Repo
from checks.standard import (
    config_readers,
    env_template_sync,
    generated_current,
    image_sources,
    pinned_config,
    seeds_current,
)
from standard import yaml_subset

MakeRepo = Callable[[dict[str, str]], Path]

READERS: dict[str, Any] = {
    "config_files": ["config.py"], "readers": [], "python_trees": ["svc"], "script_trees": [], "rust_trees": [],
    "allow": {}, "accessors": {"text": 0, "optional_text": 0}, "optional": ["optional_text"],
    "env_files": {"app.env": "app.env.template"},
    "services": [{"compose": "compose.yml", "name": "api", "config": "svc/config.py"}],
}  # fmt: skip
CONFIG = 'def load(env):\n    return env.text("DB_URL"), env.text("TOKEN"), env.optional_text("TRACE")\n'


def test_yaml_subset_resolves_anchors_and_merge_keys_and_refuses_what_it_cannot_read() -> None:
    text = "x-base: &base\n  restart: always\n  env_file: a.env\nservices:\n  api:\n    <<: *base\n    restart: never\n"
    assert yaml_subset.load(text)["services"]["api"] == {"restart": "never", "env_file": "a.env"}
    with pytest.raises(yaml_subset.YamlError, match="line 1"):
        yaml_subset.load("key: !tag value\n")


def test_an_environment_read_outside_a_config_module_fails_by_line(make_repo: MakeRepo) -> None:
    root = make_repo({
        "svc/config.py": "import os\nX = os.environ['A']\n",
        "svc/work.py": "import os\n# os.environ in a comment\nNOTE = 'os.environ'\nX = os.getenv('A')\n",
        "svc/work_test.py": "import os\nX = os.environ\n",
    })  # fmt: skip
    values = {**READERS, "services": [], "allow": {"svc/gone.py": "a stale exemption"}}
    assert config_readers.run(Repo.open(root), values) == [
        f"{config_readers.ID}: svc/work.py:4: reads the environment outside a config module; read each key once, "
        "in the service's config module (config.py)",
        f"{config_readers.ID}: stale exemption: remove svc/gone.py from allow",
    ]


def test_a_key_a_service_reads_but_is_not_supplied_fails(make_repo: MakeRepo) -> None:
    compose = "services:\n  api:\n    env_file: app.env\n    environment:\n      TOKEN: ${TOKEN:-}\n"
    root = make_repo({"compose.yml": compose, "app.env.template": "OTHER=1\n", "svc/config.py": CONFIG})
    problems = config_readers.run(Repo.open(root), READERS)
    assert [p.split(" lacks ")[1].split(" ")[0] for p in problems] == ["DB_URL", "TOKEN", "TRACE"]
    assert "interpolate it as `${TOKEN:?}`" in problems[1]


def test_a_copied_file_importing_an_uncopied_one_fails_with_its_copy_line(make_repo: MakeRepo) -> None:
    root = make_repo({
        "tree/relay/Dockerfile": "FROM node\nCOPY relay/src/ relay/src/\nCOPY shared/wire.ts shared/wire.ts\n",
        "tree/relay/src/main.ts": "import { wire } from '../../shared/wire.js';\n",
        "tree/shared/wire.ts": "export { codec } from './codec';\n",
        "tree/shared/codec.ts": "export const codec = 1;\n",
    })  # fmt: skip
    values = {"images": [{"dockerfile": "tree/relay/Dockerfile", "context": "tree"}]}
    assert image_sources.run(Repo.open(root), values) == [
        f"{image_sources.ID}: tree/relay/Dockerfile: shared/wire.ts imports ./codec, which the image does not copy. "
        "Add: COPY shared/codec.ts shared/codec.ts"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, []), (1, ["is True, must be 1"]), (False, ["is True, must be False"])],
)
def test_a_pinned_key_keeps_its_value_and_type(make_repo: MakeRepo, value: Any, expected: list[str]) -> None:
    root = make_repo({"seats.yaml": "session:\n  hosting: true\n"})
    pins = [
        {"file": "seats.yaml", "format": "yaml", "path": ["session", "hosting"], "value": value, "reason": "r"},
        {"file": "seats.yaml", "format": "yaml", "path": ["session", "gone"], "value": False, "reason": "r"},
    ]
    problems = pinned_config.run(Repo.open(root), {"pins": pins})
    assert [p.split("`session.hosting` ")[1].split(":")[0] for p in problems if "hosting" in p] == expected
    assert any("`session.gone` is absent" in p for p in problems)


def test_a_stale_generated_file_and_a_failing_check_command_fail(make_repo: MakeRepo) -> None:
    root = make_repo({"out.txt": "old\n", "gen.sh*": "#!/bin/sh\necho new\n", "check.sh*": "#!/bin/sh\nexit 1\n"})
    values = {"check_commands": [{"name": "contracts", "command": ["./check.sh"]}],
              "print_commands": [{"name": "guide", "command": ["./gen.sh"], "file": "out.txt"}]}  # fmt: skip
    problems = generated_current.run(Repo.open(root), values)
    assert "contracts" in problems[0] and "stale" in problems[0]
    assert "guide: out.txt differs" in problems[1]


def test_env_template_keys_missing_from_the_local_file_fail(make_repo: MakeRepo) -> None:
    root = make_repo({"app.env.template": "# A=0\nA=1\nB=2\n"})
    values = {"pairs": [{"template": "app.env.template", "env": "app.env"}]}
    assert "app.env is missing" in env_template_sync.run(Repo.open(root), values)[0]
    (root / "app.env").write_text("B=x\nEXTRA=1\n")
    assert env_template_sync.run(Repo.open(root), values) == [
        f"{env_template_sync.ID}: app.env lacks A, which app.env.template lists"
    ]


def test_a_failing_seed_check_fails_with_what_it_printed(make_repo: MakeRepo) -> None:
    root = make_repo({"scripts/dev*": "#!/bin/sh\necho 'seed old-notes: format 1, expected 2'\nexit 3\n"})
    (problem,) = seeds_current.run(Repo.open(root), {})
    assert "exit 3" in problem and "seed old-notes: format 1, expected 2" in problem


@pytest.mark.parametrize(
    ("check", "values"),
    [
        (image_sources, {"images": [{"dockerfile": "Dockerfile", "context": "."}]}),
        (pinned_config, {"pins": [{"file": "app.toml", "format": "toml", "path": ["a"], "value": 1, "reason": "r"}]}),
        (generated_current, {"check_commands": [], "print_commands": [{"name": "g", "command": ["true"],
                                                                       "file": "gen.md"}]}),
        (config_readers, {**READERS, "services": [{"compose": "compose.yml", "name": "api", "config": "c.py"}]}),
        (env_template_sync, {"pairs": [{"template": "app.env.template", "env": "app.env"}]}),
    ],
)  # fmt: skip
def test_a_listed_file_that_is_missing_is_a_problem_not_a_pass(make_repo: MakeRepo, check: Any,
                                                                values: dict[str, Any]) -> None:  # fmt: skip
    root = make_repo({"README.md": "nothing else\n"})
    problems = check.run(Repo.open(root), values)
    assert problems and "missing" in problems[0]
