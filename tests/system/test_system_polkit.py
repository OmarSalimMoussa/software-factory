from __future__ import annotations

import re

import pytest

from runtime.system import render

RULE = (render.HERE / render.POLKIT.source).read_text(encoding="utf-8")
UNITS = re.compile(re.search(r"if \(/(.+?)/\.test\(unit\)", RULE).group(1))  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("unit", "allowed"),
    [
        ("software-agent-run@abc123.service", True),
        ("software-stack-run@stack-9f8e.service", True),
        ("software-check-run@check-9f8e.service", True),
        ("software-root-run@x.service", False),
        ("software-agent-run@.service", False),
        ("software-warden.service", False),
        ("software-agent-daemon.service", False),
        ("ssh.service", False),
        ("xsoftware-agent-run@a.service", False),
        ("software-agent-run@a.service.d", False),
        ("software-agent-run@a/../b.service", False),
        ("software-agent-run@a.service\nssh.service", False),
        ("software-agent-run@a.timer", False),
    ],
)
def test_the_warden_may_start_only_the_three_templates(unit: str, allowed: bool) -> None:
    assert (UNITS.fullmatch(unit) is not None) is allowed


def test_the_rule_grants_only_start_stop_the_daemons_restart_and_the_slices_budget() -> None:
    granted = re.findall(r'verb === "([a-z-]+)"', RULE)

    assert sorted(granted) == ["restart", "set-property", "start", "stop"]
    assert 'unit === "@daemon_unit@" && verb === "restart"' in RULE
    assert 'unit === "@slice@" && verb === "set-property"' in RULE
    assert RULE.count("polkit.Result.YES") == 4  # the templates, the daemon's restart, the slice, the sleep locks
    assert 'subject.user !== "@warden_user@"' in RULE
