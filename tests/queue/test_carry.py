"""Carry-over: a scenario's full pass at an earlier walk stands for it in a later check of the default branch, so no
tester walks it again, while nothing it depends on has changed since."""

from __future__ import annotations

import pytest
from bundle_kit import SCENARIOS, Product
from queue_kit import World

from runtime.qa.scenarios import FOLDER, SHARED
from runtime.queue import bundle, records
from runtime.queue.book import Queue
from runtime.shared.protocol import Refused

SHARES = 'paths = ["lock.txt"]\n'
OLDER = "c-20261009T120100Z-0123456"


def _scenario(name: str) -> str:
    return f"---\n{SCENARIOS[name]}\n---\n# {name}\n\n1. Open it again.\n"


def _planned(world: World) -> tuple[list[str], list[str]]:
    answer = bundle.show(world.ctx(), {}, None)
    return [s["name"] for s in answer["scenarios"]], [s["name"] for s in answer["carried"]]


@pytest.mark.parametrize(
    ("rel", "text", "carries"),
    [
        ("docs/notes.md", None, True),
        ("api/handlers.py", None, True),
        (f"{FOLDER}/api.md", _scenario("api"), True),
        ("web/fix.js", None, False),
        ("lock.txt", None, False),
        (SHARED, 'paths = ["lock.txt", "docs/"]\n', False),
        (f"{FOLDER}/web.md", _scenario("web"), False),
        ("qa/phone-width.md", None, False),
    ],
    ids=[
        "a-path-nothing-protects",
        "a-path-another-scenario-protects",
        "another-scenario-s-file",
        "a-path-it-protects",
        "a-path-every-scenario-shares",
        "the-list-of-shared-paths",
        "its-own-file",
        "a-qa-procedure",
    ],
)
def test_a_full_pass_carries_to_a_later_walk_while_nothing_it_depends_on_changed(
    world: World, product: Product, rel: str, text: str | None, carries: bool
) -> None:
    product.commit(SHARED, text=SHARES)
    product.merge("web/page.js")
    first, _ = product.merge("api/handlers.py")
    product.check(first, web="pass", api="fail")
    product.merge(rel, text=text)

    assert _planned(world) == ((["api"], ["web"]) if carries else (["api", "web"], []))


@pytest.mark.parametrize(
    ("walks", "carries"),
    [
        ([("pass", 1)], True),
        ([("pass", 0)], False),
        ([("pass", 1), ("blocked", 1)], False),
        ([("pass", 1), ("fail", 1)], False),
        ([("fail", 1), ("pass", 1)], True),
    ],
    ids=["a-full-pass", "a-pass-short-of-its-steps", "blocked-since", "failed-since", "passed-since-a-fail"],
)
def test_only_its_newest_walk_carries_and_only_when_it_passed_every_step(
    world: World, product: Product, walks: list[tuple[str, int]], carries: bool
) -> None:
    product.commit(SHARED, text=SHARES)
    product.merge("web/page.js")
    tip, _ = product.merge("api/handlers.py")
    for result, steps in walks:
        product.check(tip, steps, web=result, api="fail")

    assert _planned(world) == ((["api"], ["web"]) if carries else (["api", "web"], []))


@pytest.mark.parametrize(
    ("shares", "said"),
    [(None, f"names no paths its scenarios share in {SHARED}"), ('paths = "lock.txt"\n', f"{SHARED}: paths is")],
    ids=["none-named", "not-a-list"],
)
def test_no_pass_carries_until_the_product_names_the_paths_its_scenarios_share(
    world: World, product: Product, shares: str | None, said: str
) -> None:
    if shares is not None:
        product.commit(SHARED, text=shares)
    product.merge("web/page.js")
    tip, _ = product.merge("api/handlers.py")
    product.check(tip, web="pass", api="fail")

    answer = bundle.show(world.ctx(), {}, None)

    assert answer["carried"] == [] and said in answer["carry_off"] and said in answer["message"]
    with pytest.raises(Refused, match="walk api, web there"):
        product.save(tip, None)


@pytest.mark.parametrize("walked", [{}, {"api": "pass"}], ids=["nothing-left-to-walk", "beside-walked-rows"])
def test_a_check_counts_each_carried_scenario_as_passed_naming_the_walk_it_carries(
    world: World, product: Product, walked: dict[str, str]
) -> None:
    product.commit(SHARED, text=SHARES)
    _, web = product.merge("web/page.js")
    first, api = product.merge("api/handlers.py")
    passed = product.check(first, web="pass", api="pass" if not walked else "fail")
    tip, docs = product.merge("docs/notes.md")
    product.slot0(tip if walked else first)  # a check that walks nothing needs no build of slot 0's
    assert ("no tester walks" in bundle.show(world.ctx(), {}, None)["message"]) is not walked

    answer = product.check(tip, **walked) if walked else product.save(tip, None)

    assert answer["cleared"] == [web, api, docs]
    saved = max(Queue.of(world.ctx()).checks()[0], key=lambda c: c.at)
    carried = ["web"] if walked else ["api", "web"]
    assert saved.carried == [records.Carried(name, passed["check"], first) for name in carried]
    assert answer["carried"] == carried


@pytest.mark.parametrize(
    ("report", "said"),
    [
        (None, "walk api there"),
        (
            f"# Check\n\n## Scenarios\n\n{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n| api | pass | 1 | x |\n\n"
            f"## Carried\n\n{records.CARRY_TABLE}\n| --- | --- | --- |\n| web | {OLDER} | 0123456 |\n",
            "the warden writes the Carried table",
        ),
    ],
    ids=["no-report-while-one-is-left", "a-report-s-own-carried-table"],
)
def test_a_check_that_skips_a_walk_carry_over_does_not_cover_is_refused_unsaved(
    product: Product, report: str | None, said: str
) -> None:
    product.commit(SHARED, text=SHARES)
    product.merge("web/page.js")
    tip, _ = product.merge("api/handlers.py")
    product.check(tip, web="pass", api="fail")

    with pytest.raises(Refused, match=said):
        product.save(tip, None if report is None else report.encode())
    assert len(Queue.of(product.world.ctx()).checks()[0]) == 1
