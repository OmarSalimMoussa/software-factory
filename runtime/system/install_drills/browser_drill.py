"""The drill that a tester's browser works where testers run it: inside the walls of the agents' daemon, which hosts
every session, it opens a page. The check's one line of verdict comes among the browser server's own log lines."""

from __future__ import annotations

from runtime.qa.browser_check import FLAG, OPENED
from runtime.shared.paths import FACTORY_BIN
from runtime.system.install_drills.evidence import Result, Rig, first_line


def tester_browser(rig: Rig) -> Result:
    name = "a tester's browser opens a page, walled in as the agents' daemon is"
    role = str(rig.settings.get_list("subagents.browser_personas")[0])
    ran = rig.as_daemon([str(FACTORY_BIN), "qa-browser", FLAG, role])
    said = [line for line in ran.out.splitlines() if line == OPENED or line.startswith("no browser: ")]
    evidence = said[-1] if said else first_line(ran.out) or f"the check printed nothing and ended {ran.code}"
    return Result(name, ran.code == 0 and said == [OPENED], f"{role}: {evidence}")
