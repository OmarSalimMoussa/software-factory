"""A check's scenarios where the dashboard draws them: one no tester walked again says it was carried, and from which
walk."""

from __future__ import annotations

from dashboard_rig import Rig

NEWEST, OLDER = "c-20261009T140500Z-a1b2c3d", "c-20261009T111000Z-77ab12e"
WORDS = "it passed at 77ab12e, and nothing it depends on has changed since"


def test_a_carried_scenario_is_drawn_apart_naming_the_walk_whose_pass_it_carries(rig: Rig) -> None:
    newest = rig.parts("/api/work?tab=checks")["checks"]["data"]["rows"][0]
    reader = rig.parts(f"/api/doc/{NEWEST}")["doc"]["data"]["html"]

    carried = [r for r in newest["results"] if r["state"] == "carried"]
    assert carried == [{"scenario": "stack", "state": "carried", "cls": "c", "title": f"stack, carried: {WORDS}"}]
    assert newest["summary"] == "17 of 19 passed, 1 carried"
    assert f'<code>stack</code> carried from <span class="doclink" data-open-doc="{OLDER}"' in reader
