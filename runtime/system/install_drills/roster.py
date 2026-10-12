"""Every drill an install runs, in the order its log lists them, and how they run: the guard and host drills one
after another, then the drills that each start a sandboxed session, side by side."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from runtime.system.install_drills import browser_drill, drills
from runtime.system.install_drills.evidence import Result, Rig

DRILLS: tuple[Callable[[Rig], Result], ...] = (
    drills.sandbox_runs,
    drills.docker_denied,
    drills.shell_write_other_worktree,
    drills.edit_other_worktree,
    drills.claude_config_by_tool,
    drills.claude_config_by_shell,
    drills.secrets_unreadable,
    drills.sockets_refused,
    drills.hosts_refused,
    drills.backstop_stops_claude,
    drills.background_session,
    drills.bubblewrap_missing,
    drills.factory_alone,
    drills.finished_subagent,
    drills.other_branch,
    drills.factory_in_sandbox,
    drills.path_shadow,
    drills.toolchain_answers,
    drills.stack_not_root,
    drills.shared_services_answer,
    drills.factory_errors_walled_off,
    drills.stack_off_tailnet,
    drills.dashboard_unreachable,
    drills.warden_runs_commands,
    drills.check_walled_in,
    browser_drill.tester_browser,
)


# Each starts at least one sandboxed session, which waits on a model: they run side by side.
SESSIONS = frozenset({drills.sandbox_runs, drills.shell_write_other_worktree, drills.secrets_unreadable,
                      drills.sockets_refused, drills.hosts_refused, drills.bubblewrap_missing,
                      drills.factory_in_sandbox, drills.path_shadow, drills.toolchain_answers,
                      drills.background_session, drills.claude_config_by_shell})  # fmt: skip


def _one(drill: Callable[[Rig], Result], rig: Rig) -> Result:
    try:
        return drill(rig)
    # telemetry: surfaced — a drill that breaks proved nothing: its `Result` fails, named, and the rest run
    except Exception as exc:  # noqa: BLE001 - a drill that breaks fails alone, and the rest still run
        return Result(drill.__name__, False, f"the drill broke: {type(exc).__name__}: {exc}")


def run_all(rig: Rig, at_once: int) -> list[Result]:
    """Every drill's result, once each, in the order of DRILLS: the guard and host drills one after another, then the
    session drills ``at_once`` at a time."""
    done = {drill: _one(drill, rig) for drill in DRILLS if drill not in SESSIONS}
    with ThreadPoolExecutor(max_workers=at_once, thread_name_prefix="drill") as pool:
        started = {drill: pool.submit(_one, drill, rig) for drill in DRILLS if drill in SESSIONS}
        done.update({drill: future.result() for drill, future in started.items()})
    return [done[drill] for drill in DRILLS]
