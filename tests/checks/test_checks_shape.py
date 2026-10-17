from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner.repo import Repo
from checks.standard import deploy_shape, qa_scenarios, repo_shape

Break = Callable[[Path], None]


def _edit(rel: str, old: str, new: str) -> Break:
    def apply(root: Path) -> None:
        path = root / rel
        text = path.read_text()
        assert old in text, (rel, old)
        path.write_text(text.replace(old, new))

    return apply


def _chmod(rel: str) -> Break:
    return lambda root: (root / rel).chmod(0o644)


def _add(rel: str, text: str) -> Break:
    def apply(root: Path) -> None:
        (root / rel).write_text(text)

    return apply


def test_the_fixture_has_the_standard_shape(fixture_product: Path) -> None:
    assert repo_shape.run(Repo.open(fixture_product), {}) == []


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (_edit("GOAL.md", "## How it speaks", "## Voice"), "GOAL.md has no '## How it speaks' heading"),
        (_edit("GOAL.md", "- G2 People", "- G3 People"), "GOAL.md names G3 twice"),
        (_edit("GOAL.md", "Done when searching", "Once searching"), "G2's outcome does not read '<outcome>. Done when"),
        (_edit("GOAL.md", "- G2 People", "- 2. People"), "does not read '- <name> <outcome>'"),
        (_edit(".gitignore", "queue/\n", ""), ".gitignore does not ignore queue/"),
        (_edit("scripts/dev", '"reports")', '"reports", "logs")'), "lists logs, which is not a standard verb"),
        (_edit("scripts/dev", '"verbs", "test",', '"verbs",'), "scripts/dev verbs does not list test"),
        (_chmod("scripts/dev"), "scripts/dev is not executable"),
        (_add("check-values/lint.toml", "x = 1\n"), "check-values/lint.toml feeds no standard check"),
    ],
)
def test_a_broken_shape_fails_naming_the_problem(
    fixture_product: Path, commit: Callable[[Path, str], None], damage: Break, said: str
) -> None:
    damage(fixture_product)
    commit(fixture_product, "damage")
    problems = repo_shape.run(Repo.open(fixture_product), {})
    assert any(said in p for p in problems), problems


@pytest.mark.parametrize(
    ("needs", "sign_in", "said"),
    [
        ("nothing", "`factory slot sign-in\n  qa@notes.test`", "signs in as qa@notes.test but needs names no person"),
        ("seed notes", "`factory slot sign-in qa@notes.test`", "signs in as qa@notes.test"),
        ("seed notes, person admin", "`factory slot sign-in qa@notes.test`", None),
        ("nothing", "`factory slot sign-in <your address>`", None),
    ],
    ids=["over-a-line-break", "beside-a-seed", "named", "a-placeholder"],
)
def test_a_scenario_that_signs_in_as_a_named_address_needs_that_person(
    fixture_product: Path, commit: Callable[[Path, str], None], needs: str, sign_in: str, said: str | None
) -> None:
    front = f"---\nprotects: app/notes.py\nneeds: {needs}\nruns: in-turn\n---\n"
    _add("qa/scenarios/sign-in.md", f"{front}# Sign in\n\nSign in: {sign_in}.\n\n1. Open it.\n")(fixture_product)
    commit(fixture_product, "a scenario")
    for changed in (None, ["qa/scenarios/sign-in.md"]):  # every file, or a branch that changed this one
        found = qa_scenarios.run(Repo.open(fixture_product, changed), {})
        mine = "\n".join(p for p in found if "sign-in.md" in p)
        assert (said or "") in mine and bool(mine) is (said is not None), mine
    # A branch that changed no scenario is not failed on one it never touched.
    assert not [p for p in qa_scenarios.run(Repo.open(fixture_product, ["app/notes.py"]), {}) if "sign-in.md" in p]


def _remove(rel: str) -> Break:
    return lambda root: (root / rel).unlink()


def _derive_twice(root: Path) -> None:
    """app builds FROM mid, which builds FROM base: two levels."""
    toml = root / "factory.toml"
    extras = ", ".join(f'{{ name = "{n}", dockerfile = "{n}/Dockerfile", context = "." }}' for n in ("mid", "base"))
    toml.write_text(toml.read_text().replace("extra_images = []", f"extra_images = [{extras}]"))
    for name, base in (("mid", "notes-base:${NOTES_IMAGE_TAG}"), ("base", None), ("app", "notes-mid:$NOTES_IMAGE_TAG")):
        (root / name).mkdir(exist_ok=True)
        (root / name / "Dockerfile").write_text(f"FROM ghcr.io/owner/{base}\n" if base else "FROM busybox:1.37\n")


def test_the_fixture_has_the_standard_deploy_shape(fixture_product: Path) -> None:
    assert deploy_shape.run(Repo.open(fixture_product), {}) == []


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (_edit("factory.toml", 'name = "notes"', 'name = "Notes"'), "factory.toml: [product].name: 'Notes' is not"),
        (_edit("factory.toml", '"ghcr.io/owner"', '"registry.example.com/owner"'),
         "[product].registry is registry.example.com/owner, but CI pushes only to ghcr.io/<owner>"),
        (_chmod("scripts/smoke-check"), "factory.toml names scripts/smoke-check as a hook, but it is not executable"),
        (_edit("factory.toml", 'expect = [{ service = "app"', 'expect = [{ service = "db"'),
         "[smoke] names the service db, which compose.yml does not build"),
        (_edit("factory.toml", "extra_images = []",
               'extra_images = [{ name = "worker", dockerfile = "worker/Dockerfile", context = "." }]'),
         "worker/Dockerfile is missing"),
        (_derive_twice, "app derives from mid, which itself derives from base"),
        (_edit("compose.yml", "notes-app:", "notes-web:"), "service app builds an image, so its image must be"),
        (_edit("compose.yml", "    ports:", "    volumes:\n      - ./app/seed.json:/seed.json\n    ports:"),
         "service app mounts ./app/seed.json, which a deploy does not copy"),
        (_edit("factory.toml", '"compose.yml", "notes', '"compose.yml", "Caddyfile", "notes'),
         "[deploy].files names Caddyfile, which is not tracked"),
        (_edit("factory.toml", '"compose.yml", "notes.env.template"]', '"compose.yml"]'),
         "[deploy].files lacks notes.env.template"),
        (_add("qa/README.md", "Pull ghcr.io/owner/notes-worker:latest first.\n"),
         "qa/README.md names the image ghcr.io/owner/notes-worker, which CI never builds"),
        (_edit("notes.env.template", "NOTES_ORIGIN=\n", ""), "[env.set] writes NOTES_ORIGIN, which notes.env.template"),
        (_edit("compose.yml", '"127.0.0.1:${NOTES_PORT:?}:8000"', '"${NOTES_PORT:?}:8000"'),
         "service app publishes '${NOTES_PORT:?}:8000' beyond this machine"),
        (_edit(".dockerignore", "**/.claude\n", ""), ".dockerignore does not leave out **/.claude, so the build of"),
        (_remove(".dockerignore"), ".dockerignore is missing (or not committed), so the build of app"),
        (_edit(".dockerignore", "**/.claude\n", "**/.claude\n!README.md\n!**/keep\n"),
         ".dockerignore lets !**/keep back in, and a wildcard"),
        (_add("app/Dockerfile.dockerignore", "**/__pycache__\n"), "app/Dockerfile.dockerignore does not leave out"),
    ],
    ids=["settings", "registry", "hook", "smoke", "extra-image", "two-levels", "image-name", "bind-mount",
         "untracked-file", "deploy-files", "named-image", "env-key", "port", "claude-folders", "no-ignore-file",
         "wildcard-back-in", "dockerfiles-own-ignore"],
)  # fmt: skip
def test_a_product_off_the_standard_deploy_fails_naming_what_to_do(
    fixture_product: Path, commit: Callable[[Path, str], None], damage: Break, said: str
) -> None:
    damage(fixture_product)
    commit(fixture_product, "damage")
    problems = deploy_shape.run(Repo.open(fixture_product), {})
    assert any(said in p for p in problems), problems
