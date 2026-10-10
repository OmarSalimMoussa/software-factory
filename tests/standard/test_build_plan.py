"""The build plan CI reads: a derived image builds after its base, one level only, and build args never need the
product's env."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from standard import plan

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"
COMMIT, FACTORY = "0123456789abcdef0123456789abcdef01234567", "89abcdef0123456789abcdef0123456789abcdef"
REF = "ghcr.io/owner/notes-"


def product(root: Path, *, app_from: str, extras: dict[str, str], args: str) -> Path:
    """The fixture's settings and stack, with ``app`` built FROM ``app_from`` and each extra image FROM its line."""
    root.mkdir()
    for name in ("factory.toml", "compose.yml", "notes.env.template"):
        shutil.copy(FIXTURE / name, root / name)
    listed = ", ".join(f'{{ name = "{n}", dockerfile = "{n}/Dockerfile", context = "." }}' for n in extras)
    toml = root / "factory.toml"
    toml.write_text(toml.read_text().replace("extra_images = []", f"extra_images = [{listed}]"))
    compose = root / "compose.yml"
    compose.write_text(compose.read_text().replace("        NOTES_BUILD_COMMIT: ${NOTES_IMAGE_TAG:?}\n", args))
    for name, base in {"app": app_from, **extras}.items():
        (root / name).mkdir()
        (root / name / "Dockerfile").write_text(f"ARG NOTES_IMAGE_TAG=dev\nFROM {base}\nCOPY . /srv\n")
    return root


@pytest.mark.parametrize(
    ("app_from", "extras", "args", "outcome"),
    [
        (
            REF + "base:${NOTES_IMAGE_TAG}",
            {"base": "python:3.12-slim"},
            '        NOTES_BUILD_COMMIT: "${NOTES_BUILD_COMMIT:?}"\n        MODE: "$$quiet"\n',
            {"base": [{"name": "base", "after": None, "args": {"NOTES_BUILD_COMMIT": "01234567"}}],
             "derived": [{"name": "app", "after": "base", "args": {"NOTES_BUILD_COMMIT": "01234567", "MODE": "$quiet",
                                                                    "NOTES_IMAGE_TAG": "01234567"}}]},
        ),
        (
            REF + "mid:${NOTES_IMAGE_TAG:-dev}",
            {"mid": REF + "base:$NOTES_IMAGE_TAG", "base": "python:3.12-slim"},
            "",
            "app derives from mid, which itself derives from base",
        ),
        (
            "python:3.12-slim",
            {},
            "        API_URL: ${API_URL}\n",
            "build arg API_URL names ${API_URL}, which CI cannot fill",
        ),
        (REF + "base:latest", {"base": "python:3.12-slim"}, "", "so its tag must be ${NOTES_IMAGE_TAG}"),
    ],
    ids=["derived-after-base", "two-levels", "env-arg", "fixed-tag"],
)  # fmt: skip
def test_a_derived_image_builds_after_its_base_and_nothing_needs_the_products_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_from: str, extras: dict[str, str], args: str,
    outcome: dict[str, list[dict[str, object]]] | str,
) -> None:  # fmt: skip
    root = product(tmp_path / "repo", app_from=app_from, extras=extras, args=args)
    output = tmp_path / "github-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["--repo", str(root), "--commit", COMMIT, "--factory-commit", FACTORY, "--out", str(tmp_path / "plan.json")]
    if isinstance(outcome, str):
        with pytest.raises(plan.PlanError) as refused:
            plan.make(root, COMMIT, FACTORY)
        assert any(outcome in p for p in refused.value.problems), refused.value.problems
        assert plan.main([*argv, "--github-output"]) == 1 and output.read_text() == ""
        return
    assert plan.main([*argv, "--github-output"]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["short"] == "01234567" and lines["has_derived"] == "true"
    for group, wanted in outcome.items():
        got = json.loads(lines[group])
        assert [{k: image[k] for k in ("name", "after", "args")} for image in got] == wanted
    written = json.loads((tmp_path / "plan.json").read_text())
    assert [i["name"] for i in written["images"]] == ["base", "app"]
    assert written["files"] == ["compose.yml", "notes.env.template", "factory.toml"]
    assert written["images"][1]["ref"] == REF + "app" and written["factory_commit"] == FACTORY
