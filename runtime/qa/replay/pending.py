"""The warden's replays pass: every run taken into the evidence gets its replay built once, off the tick.

A run's inputs are the warden's own: its entry in the evidence index names the task or the check, the head, the
tester's kind and agent, and the session the tester ran in, whose transcript holds the tester's under ``subagents/``.
That session may run on as a copy under a new id since. The scenarios are read as the tested head holds them, from
the product's repo. A run that cannot be replayed says why in its replay's place and is not tried again.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from runtime.guard.transcript import subagent_transcript
from runtime.qa import evidence, findings, steps
from runtime.qa.replay.beats import Shares
from runtime.qa.replay.build import DATA, FOLDER, Inputs, build, failed
from runtime.qa.replay.timeline import Timings
from runtime.qa.replay.trace import ReplayError
from runtime.qa.scenarios import FOLDER as SCENARIOS
from runtime.qa.scenarios import Scenario, parse
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.spend import cost

log = logging.getLogger("factory.warden")


def build_pending(ctx: Context, roots: Sequence[Path]) -> int:
    """Build the replay of every run that has none yet; how many were built or failed."""
    done = 0
    for entry in evidence.drawn(ctx.places):
        run = evidence.folder(ctx.places, entry)
        if not run.is_dir() or (run / FOLDER / DATA).exists():
            continue
        which = (entry.get("tester"), entry.get("run"), evidence.of(entry))
        try:
            build(inputs(ctx, roots, entry, run))
        except ReplayError as exc:
            log.warning("warden: the replay of %s run %s for %s could not be built (ReplayError)", *which)
            _give_up(run, str(exc), which)
        # A run is the tester's own writing: whatever breaks on one is that run's alone, so the next is still built.
        except Exception as exc:
            log.exception("warden: the replay of %s run %s for %s broke (%s)", *which, type(exc).__name__)
            _give_up(run, f"it could not be built ({type(exc).__name__})", which)
        done += 1
    return done


def _give_up(run: Path, reason: str, which: tuple[Any, ...]) -> None:
    try:
        failed(run, reason)
    # A run whose failure cannot be written is tried again on the next pass; the runs after it still go.
    except OSError as exc:
        log.exception(
            "warden: the failed replay of %s run %s for %s could not be noted (%s)", *which, type(exc).__name__
        )


def inputs(ctx: Context, roots: Sequence[Path], entry: dict[str, Any], run: Path) -> Inputs:
    agent = entry.get("agent")
    if not agent:
        raise ReplayError("which tester walked it is not known, so its transcript cannot be found")
    ran_in = str(entry["session"])
    session = next((s for s in registry.read(ctx.places).values() if ran_in in (s.id, s.copied_from)), None)
    if session is None:
        raise ReplayError("the session that ran this tester is no longer known, so its transcript cannot be found")
    parent = cost.transcript_of(roots, session.worktree, ran_in)
    if parent is None:
        raise ReplayError("the tester's transcript is gone, so its replay cannot be built")
    transcript = subagent_transcript(parent, str(agent))
    if not transcript.is_file():
        raise ReplayError("the tester's transcript is gone, so its replay cannot be built")
    settings = ctx.settings()
    head = str(entry["head"])

    def scenario(name: str) -> Scenario | None:
        code, text = ctx.launcher.agent_command(
            ["git", "-C", settings.get_str("repo.path"), "show", f"{head}:{SCENARIOS}/{name}.md"],
            settings.get_str("repo.path"),
        )
        if code != 0:
            return None
        return parse(name, text, settings.get_int("qa.max_steps"), settings.get_int("qa.max_items"))[0]

    return Inputs(
        run=run,
        transcript=transcript,
        records=steps.of_agent(ctx, session.id, str(agent)),
        findings=findings.of_agent(ctx, str(entry["task"]), str(agent)) if "task" in entry else [],
        scenario=scenario,
        timings=Timings(
            settings.get_float("replay.long_wait_seconds"),
            settings.get_float("replay.wait_tail_seconds"),
            settings.get_float("replay.navigation_tail_seconds"),
        ),
        recorder=(
            settings.get_float("replay.recorder_before_seconds"),
            settings.get_float("replay.recorder_after_seconds"),
        ),
        shares=Shares(settings.get_float("replay.desktop_view_share"), settings.get_float("replay.read_part_share")),
        secret_seconds=settings.get_int("replay.secret_seconds"),
    )
