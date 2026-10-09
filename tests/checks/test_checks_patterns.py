from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from checks.runner.repo import Repo
from checks.runner.values import ValuesError
from checks.standard import (
    computation_owners,
    js_import_rules,
    mirror_markers,
    mirrored_constants,
    network_door,
    no_console,
    quiet_modules,
    vocabulary_homes,
)

MakeRepo = Callable[[dict[str, str]], Path]

DOOR = """
[[doors]]
name = "server HTTP"
scans = ["app/"]
suffixes = [".py"]
calls = ['\\burllib\\.request\\.urlopen\\b']
unless = []
owners = ["app/net.py"]
code_only = true
remedy = "call through app/net.py"
[doors.allow]
"""
MIRRORS = """
skip = []
claims = [{CLAIMS}]
[copies]
server = ["app/"]
web = ["web/"]
generated_markers = ["Generated from"]
rows = []
"""


def run(module: ModuleType, make_repo: MakeRepo, files: dict[str, str]) -> list[str]:
    repo = Repo.open(make_repo(files))
    problems: list[str] = module.run(repo, repo.values(module.VALUES_FILE, module.VALUES))
    return problems


def test_network_door_refuses_a_client_outside_the_door_but_not_one_a_docstring_names(make_repo: MakeRepo) -> None:
    problems = run(network_door, make_repo, {
        "check-values/network-door.toml": DOOR,
        "app/net.py": "import urllib.request\nurllib.request.urlopen('x', timeout=1)\n",
        "app/sync.py": '"""Never call urllib.request.urlopen here."""\nimport urllib.request\n'
                       "urllib.request.urlopen('x')\n",
    })  # fmt: skip
    assert len(problems) == 1 and problems[0].startswith("app/sync.py:3: opens its own link")


def test_an_allowed_file_that_answers_no_line_is_stale(make_repo: MakeRepo) -> None:
    problems = run(network_door, make_repo, {
        "check-values/network-door.toml": DOOR + '"app/quiet.py" = "an upstream client with its own deadline"\n',
        "app/net.py": "urllib.request.urlopen('x', timeout=1)\n",
        "app/quiet.py": "VALUE = 1\n",
    })  # fmt: skip
    assert problems == [
        "network-door (server HTTP): stale exemption: app/quiet.py answers no line; remove it from allow"
    ]


def test_a_console_call_in_a_comment_or_string_is_no_call(make_repo: MakeRepo) -> None:
    problems = run(no_console, make_repo, {
        "check-values/no-console.toml": 'roots = ["web/"]\nsuffixes = [".js"]\nlog_modules = ["web/log.js"]\n'
                                        'remedy = "log through web/log.js"\n',
        "web/log.js": "export const warn = (m) => console.warn(m);\n",
        "web/app.js": "// console.log(x) was here\nconst s = 'console.error(';\n/* console.info(1)\n*/\n"
                      "console.debug(s);\n",
    })  # fmt: skip
    assert [p.split(" — ")[0] for p in problems] == ["web/app.js:5: calls the console directly"]


@pytest.mark.parametrize(
    ("statement", "broken"),
    [
        ('import { Session } from "./session.js";', True),
        ('import type { Session } from "./session.js";', False),
        ('import { type Session } from "./session.js";', False),
        ('import { isActive } from "./session.js";', False),
        ('import { isActive, start } from "./session.js";', True),
        ('// import { start } from "./session.js";', False),
        ('const s = await import("./session");', True),
    ],
)
def test_js_import_rules_let_types_and_named_guards_past(make_repo: MakeRepo, statement: str, broken: bool) -> None:
    problems = run(js_import_rules, make_repo, {
        "check-values/js-import-rules.toml": 'exempt = []\n[[rules]]\nname = "one owner"\nimporters = ["web/"]\n'
            'targets = ["web/session.js"]\nowners = ["web/owner.js"]\ntypes_allowed = true\n'
            'allowed_names = ["isActive"]\nremedy = "go through web/owner.js"\n',
        "web/session.js": "export const start = 1;\n",
        "web/owner.js": 'import { start } from "./session.js";\n',
        "web/page.js": statement + "\n",
    })  # fmt: skip
    assert bool(problems) == broken, problems


def test_an_unclaimed_mirror_declaration_fails_and_a_claim_with_no_declaration_is_stale(make_repo: MakeRepo) -> None:
    claim = '{path = "app/old.py", phrase = "keep in sync", fragment = "gone", covered_by = "debt:a reason"}'
    problems = run(mirror_markers, make_repo, {
        "check-values/mirror-markers.toml": MIRRORS.replace("{CLAIMS}", claim),
        "check-values/mirrored-constants.toml": "groups = []\n",
        "app/old.py": "X = 1\n",
        "app/limits.py": "LIMIT = 5  # must mirror web/limits.js\n",
        "web/limits.js": "export const LIMIT = 5;\n",
    })  # fmt: skip
    assert len(problems) == 2
    assert problems[0].startswith("app/limits.py:1: declared mirror is not claimed")
    assert problems[1].startswith("mirror-markers: stale claim: app/old.py")


def test_a_server_vocabulary_copied_into_browser_code_must_be_covered(make_repo: MakeRepo) -> None:
    problems = run(mirror_markers, make_repo, {
        "check-values/mirror-markers.toml": MIRRORS.replace("{CLAIMS}", ""),
        "check-values/mirrored-constants.toml": "groups = []\n",
        "app/states.py": 'STATES = ("open", "shut", "gone")\n',
        "web/states.js": "const STATES = ['gone', 'open', 'shut'];\n",
        "web/made.js": "// Generated from app/states.py\nconst COPY = ['gone', 'open', 'shut'];\n",
    })  # fmt: skip
    assert [p.split(" — ")[0] for p in problems] == ["web/states.js:1: STATES copies app/states.py:1 STATES"]


GROUP = """
[[groups]]
name = "page size"
kind = "int"
casefold = false
sides = [
  {{path = "app/limits.py", pattern = '^PAGE = (?P<expr>.+)$', items = "strings"}},
  {{path = "web/limits.js", pattern = '^export const PAGE = (?P<expr>[^;]+);$', items = "strings"}},
]
"""


@pytest.mark.parametrize(("web", "agree"), [("2 * 512", True), ("1000", False)])
def test_mirrored_values_must_agree(make_repo: MakeRepo, web: str, agree: bool) -> None:
    problems = run(mirrored_constants, make_repo, {
        "check-values/mirrored-constants.toml": GROUP.format(),
        "app/limits.py": "PAGE = 1024\n",
        "web/limits.js": f"export const PAGE = {web};  // page size\n",
    })  # fmt: skip
    disagree = "mirrored-constants (page size): web/limits.js has 1000, app/limits.py has 1024"
    assert problems == ([] if agree else [disagree])


def test_a_word_outside_its_home_fails_even_in_markdown_and_behind_an_underscore(make_repo: MakeRepo) -> None:
    problems = run(vocabulary_homes, make_repo, {
        "check-values/vocabulary-homes.toml": '[[vocabularies]]\nname = "tools"\nwords = ["browser_click"]\n'
            'home = ["app/tools/"]\nsuffixes = []\nallow = {}\nremedy = "take it from app.tools"\n',
        "app/tools/names.py": 'CLICK = "browser_click"\n',
        "docs/guide.md": "Call `mcp_browser_click` to press a button.\n",
        "app/other.py": "browser_clicker = 1\n",
    })  # fmt: skip
    assert [p.split(" — ")[0] for p in problems] == [
        "docs/guide.md:1: tools word 'browser_click' written outside its home"
    ]


QUIET = """loud = ['\\blogger\\.(warning|error|exception|critical)\\(']
remedy = "show it on the page"
[modules]
"app/judge.py" = "a verdict is the user's to read, never a fault"
"""


def test_a_quiet_module_that_logs_a_warning_fails(make_repo: MakeRepo) -> None:
    problems = run(quiet_modules, make_repo, {
        "check-values/quiet-modules.toml": QUIET,
        "app/judge.py": "logger.info('fine')\n# logger.warning('a note')\nlogger.warning('loud')\n",
    })  # fmt: skip
    assert [p.split(" — ")[0] for p in problems] == ["app/judge.py:3: logs above info in a quiet module"]


def test_a_listed_module_that_does_not_exist_fails_loudly(make_repo: MakeRepo) -> None:
    problems = run(quiet_modules, make_repo, {"check-values/quiet-modules.toml": QUIET, "app/other.py": "X = 1\n"})
    assert problems == ["quiet-modules: app/judge.py does not exist; fix or remove it"]


def test_a_computation_matches_only_when_every_pattern_of_a_row_does(make_repo: MakeRepo) -> None:
    problems = run(computation_owners, make_repo, {
        "check-values/computation-owners.toml": '[[computations]]\nname = "time rounding"\nscans = ["app/"]\n'
            "suffixes = []\nwhen = [['\\bround\\(', 't_ms']]\ncode_only = false\nremedy = \"use app.clock\"\n"
            '[computations.owners]\n"app/clock.py" = "the one rounding rule"\n',
        "app/clock.py": "def key(t_ms): return round(t_ms)\n",
        "app/money.py": "total = round(price)\n",
        "app/draw.py": "x = round(t_ms / 1000)\n",
    })  # fmt: skip
    assert [p.split(" — ")[0] for p in problems] == ["app/draw.py:1: computes time rounding outside its owners"]


def test_a_regex_that_does_not_compile_fails_when_the_values_load(make_repo: MakeRepo) -> None:
    root = make_repo({"check-values/quiet-modules.toml": QUIET.replace("\\(", "(")})
    with pytest.raises(ValuesError, match="loud\\[0\\] does not compile"):
        Repo.open(root).values(quiet_modules.VALUES_FILE, quiet_modules.VALUES)
