"""What the install console's page says beside a run: whether the factory is installed and answers, what the
install form offers, and how each action ended. Every sentence the page draws is made here or in the run."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.dashboard.client import Warden, WardenDown
from runtime.shared import network, store
from runtime.shared import settings as settings_module
from runtime.shared.paths import Places
from runtime.system import ci_check, github_keys, healthchecks, install, layout, uninstall
from runtime.system.host import Account, Host
from runtime.system.install_console.console_run import FAILED, Run
from runtime.system.install_steps import steps_wire
from runtime.system.job import Install


def _action(ident: str, label: str, tone: str) -> dict[str, str]:
    return {"id": ident, "label": label, "tone": tone}


def read_record(places: Places) -> dict[str, Any] | None:
    """The last finished install's record; None when there is none. Raises ValueError when it cannot be used."""
    path = layout.install_record(places.state)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    # telemetry: expected — no install has finished on this machine
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or not all(isinstance(record.get(key), str) for key in ("commit", "subject")):
        raise ValueError("it names no commit and its subject")
    drills = record.get("drills")
    if not isinstance(drills, list) or not all(
        isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("ok"), bool)
        and isinstance(d.get("evidence"), str) for d in drills
    ):  # fmt: skip
        raise ValueError("its drills are not a list of verdicts")
    return record


# ---- status ------------------------------------------------------------------------------------------------------


def status(places: Places, host: Host) -> dict[str, Any]:
    path = layout.install_record(places.state)
    blank: dict[str, Any] = {"installed": False, "headline": "Not installed", "tone": "", "facts": [], "drills": None,
                             "services": [], "address": None, "problem": ""}  # fmt: skip
    try:
        record = read_record(places)
    # telemetry: surfaced — the status shows the owner the `problem` with the record
    except (OSError, ValueError) as exc:
        return {**blank, "installed": True, "headline": "Installed", "tone": "warn",
                "problem": f"The install record {path} cannot be read ({exc}): install again."}  # fmt: skip
    if record is None:
        return blank
    when = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [
        {"label": "Commit", "value": record["commit"][:12], "sub": record["subject"]},
        {"label": "Installed", "value": when, "sub": ""},
    ]
    verdicts = record["drills"]
    failed = [d for d in verdicts if not d["ok"]]
    if failed:
        summary = f"{len(failed)} of {len(verdicts)} drills failed at the last install."
    else:
        summary = f"All {len(verdicts)} drills passed at the last install."
    drills = {"summary": summary, "ok": not failed,
              "failed": [{"name": d["name"], "evidence": d["evidence"]} for d in failed]}  # fmt: skip
    services, problem = _services(places)
    tone = "good" if not problem and all(s["ok"] for s in services) and not failed else "warn"
    return {"installed": True, "headline": "Installed", "tone": tone, "facts": facts, "drills": drills,
            "services": services, "address": address(host), "problem": problem}  # fmt: skip


# What the status reads of the installed factory's published settings. The install may be an older release's, so
# these keys alone are read: the rest of its settings may follow another schema.
_STATUS_KEYS = ("watch.http_timeout_seconds", "dashboard.warden_seconds", "dashboard.port")


def _services(places: Places) -> tuple[list[dict[str, Any]], str]:
    try:
        values = settings_module.read_some(store.read_json(places.state / "public" / "settings.json"), _STATUS_KEYS)
    # telemetry: surfaced — the status draws `why` nothing was checked
    except (OSError, store.StoreError, settings_module.SettingsError) as exc:
        why = f"The warden's published settings cannot be read ({type(exc).__name__})"
        return [], f"{why}, so the warden, the dashboard and the shared services were not checked."
    timeout = float(values["watch.http_timeout_seconds"])
    try:
        Warden(places.owner_socket, lambda: float(values["dashboard.warden_seconds"])).call(
            "snapshot", {"want": ["factory"]}
        )
        warden = True
    # telemetry: surfaced — the status draws the `warden` as not answering
    except WardenDown:
        warden = False
    port = values["dashboard.port"]
    found = {
        "The warden": warden,
        "The dashboard": _answers(f"http://127.0.0.1:{port}/", timeout, any_status=True),
        "GlitchTip": _answers(layout.GLITCHTIP_HEALTH, timeout, any_status=False),
        "Langfuse": _answers(layout.LANGFUSE_HEALTH, timeout, any_status=False),
    }
    return [{"name": name, "ok": ok, "text": "Answers" if ok else "Does not answer"} for name, ok in found.items()], ""


def _answers(url: str, timeout: float, *, any_status: bool) -> bool:
    try:
        status = network.call(url, timeout, follow=True).status
    # telemetry: surfaced — a service that does not answer is drawn so, from what `_answers` returns
    except (OSError, ValueError):
        return False
    return any_status or status == 200


def address(host: Host) -> dict[str, str] | None:
    """The dashboard on the tailnet, as the page links it; None when tailscale names no machine."""
    url = steps_wire.dashboard_address(host)
    return {"label": "Open the dashboard", "url": url, "text": url} if url else None


# ---- the install form --------------------------------------------------------------------------------------------


def install_form(
    offered: dict[str, Any],
    ci: dict[str, Any] | None,
    repo: Path | None,
    record: dict[str, Any] | None,
    checked: dict[str, bool],
    places: Places,
) -> dict[str, Any]:
    """The form, with ``checked`` saying which choices start on (the console's own flags). An installed factory is
    upgraded: the form says from which commit, and what the upgrade keeps."""
    # --repo, else the checkout the last install named; with neither, the field is empty for the owner to fill.
    named = record.get("product") if record else None
    product = str(repo) if repo else named if isinstance(named, str) else ""
    verb = "Upgrade" if record else "Install"
    watched = healthchecks.keys_path(places.state).exists()
    if record and watched:
        checks = {"id": "healthchecks", "label": "Set up the Healthchecks.io checks again", "checked": False,
                  "hint": "They are set up, and the upgrade keeps them. Check this only to make them again with a "
                          "new API key."}  # fmt: skip
    else:
        checks = {"id": "healthchecks", "label": "Create the Healthchecks.io checks",
                  "checked": checked["healthchecks"],
                  "hint": "They tell you when the warden or the orchestrator goes quiet. Asks for an API "
                          "key."}  # fmt: skip
    keep = {"id": "keep", "label": "Keep a failed first install", "checked": checked["keep"],
            "hint": "Leave what it made in place to look at, instead of taking it back."}  # fmt: skip
    return {
        "heading": verb,
        "commit": {"label": "Upgrade to" if record else "Factory commit", **offered, "ci": ci},
        "installed": {"label": "Installed now", "short": str(record["commit"])[:7], "subject": record["subject"]}
        if record else None,
        "kept": {"label": "Kept as it is", "items": kept(places, watched)} if record else None,
        "repo": {"label": "Product checkout", "value": product,
                 "hint": "Your own checkout of the product the factory works on."},  # fmt: skip
        "choices": [checks] if record else [checks, keep],
        "dry": {"label": "Dry run", "checked": checked["dry"],
                "hint": "Go through every step, reading this computer and changing nothing."},
        "button": verb,
        "dry_button": "Dry run",
    }  # fmt: skip


def kept(places: Places, watched: bool) -> list[str]:
    """What an upgrade keeps, in the owner's words: everything the installed factory holds."""
    try:
        changed = len(settings_module.flatten(tomllib.loads(places.overrides.read_text(encoding="utf-8"))))
    # telemetry: expected — no saved settings: the owner changed none, so nothing is kept
    except FileNotFoundError:
        changed = 0
    # telemetry: surfaced — the owner reads in what `kept` returns that the settings cannot be read
    except (OSError, ValueError) as exc:
        return [f"Your settings: their file cannot be read ({type(exc).__name__}), and the upgrade will say why"]
    settings = (
        f"Your settings: the {changed} {'value' if changed == 1 else 'values'} you changed, the rest at the "
        "new release's defaults"
        if changed
        else "Your settings: all at the new release's defaults, as now"
    )
    return [
        settings,
        "Your Claude accounts, GitHub token and keys",
        "The 6-digit code on your phone",
        *(["The Healthchecks.io checks"] if watched else []),
        "The agents' copy of the product, its queue and their paused work",
        "The shared services and their data",
    ]


_CI = {
    ci_check.PASSED: ("good", "CI passed", "The factory's CI passed on this commit."),
    ci_check.RUNNING: ("warn", "CI running", "The factory's CI is still running on this commit. Install it once CI "
                       "passes."),
    ci_check.FAILED: ("bad", "CI failed", "The factory's CI failed on this commit. Fix it and push the fix, then start "
                      "the console with that commit."),
}  # fmt: skip


def ci_view(found: ci_check.Verdict) -> dict[str, Any]:
    """The offered commit's CI, as its row draws it: a chip, a sentence, and a link to each check not passed."""
    if found.state == ci_check.UNKNOWN:
        tone, label, text = "warn", "CI not checked", sentence(found.reason).rstrip(".") + "."
    else:
        tone, label, text = _CI[found.state]
    links = [{"label": f"{check.name}: {check.what}", "url": check.url} for check in found.open]
    return {"state": found.state, "tone": tone, "label": label, "text": text, "links": links}


def checkout_offer(host: Host, owner: Account, factory: Path, given: str | None) -> dict[str, Any]:
    """The one commit this console installs from a checkout: the one it runs from, sealed when it started."""
    choice = commit_choice(host, owner, factory, given)
    if choice["problem"]:
        return choice
    return {**choice, "note": "To install another commit, start the console again with --commit <sha>."}


def checkout_ci(host: Host, owner: Account, factory: Path, offered: dict[str, Any]) -> dict[str, Any] | None:
    """The offered commit's CI, asked of GitHub; None when no commit is offered."""
    if offered["problem"]:
        return None
    origin = ci_check.origin(host, owner.name, factory)
    return ci_view(ci_check.verdict(host, owner, origin, offered["value"]))


def release_offer(manifest: dict[str, Any]) -> dict[str, Any]:
    """The commit a release file holds, which is the one it installs."""
    sha = str(manifest["commit"])
    note = f"This release was built on {manifest['built']}. To install another, download its release and run that."
    return {"value": sha, "short": sha[:7], "subject": manifest["subject"], "note": note, "problem": ""}


RELEASE_CI = {"state": ci_check.PASSED, "tone": "good", "label": "CI passed", "links": [],
              "text": "The factory's CI built this release after every check passed."}  # fmt: skip


def commit_choice(host: Host, owner: Account, factory: Path, given: str | None) -> dict[str, Any]:
    git = ["git", "-C", str(factory)]
    if given:
        ref, note = given, "The commit you named with --commit"
    else:
        branch = github_keys.default_branch(host, owner, factory)
        if not branch:
            problem = (f"The default branch of {factory} cannot be found: sign in to the GitHub CLI (gh auth login), "
                       "or name a commit with --commit.")  # fmt: skip
            return {"value": "", "short": "", "subject": "", "note": "", "problem": problem}
        ref, note = branch, f"The newest commit on {branch}"
    code, out = host.query([*git, "log", "-1", "--format=%H%n%h%n%s", ref, "--"], user=owner.name)
    found = out.strip().split("\n", 2)
    if code != 0 or len(found) != 3:
        return {"value": "", "short": "", "subject": "", "note": "", "problem": f"{ref} is not a commit in {factory}."}
    sha, short, subject = found
    return {"value": sha, "short": short, "subject": subject, "note": note, "problem": ""}


# ---- how an action ended ----------------------------------------------------------------------------------------


def _stopped(run: Run, what: str) -> str:
    with run.cond:
        phase = next((row.title for row in run.rows if row.state == FAILED), None)
    return f"{what} while {phase[:1].lower() + phase[1:]}." if phase else f"{what} before it began."


def sentence(problem: str) -> str:
    return problem[:1].upper() + problem[1:]


def _ending(run: Run, ok: bool, headline: str, then: str, actions: list[dict[str, str]], *, detail: str = "",
            links: list[dict[str, str]] | None = None) -> dict[str, Any]:  # fmt: skip
    log = run.journal.path
    shown = [_action("log", "Show the log", "")] if log and not ok else []
    return {"ok": ok, "headline": headline, "detail": detail, "next": then,
            "log": f"Every detail is in the log: {log}" if log else "", "links": links or [],
            "actions": [*shown, *actions]}  # fmt: skip


DONE = _action("dismiss", "Done", "")


def install_ending(ctx: Install, problem: str | None, keep: bool, run: Run, host: Host) -> dict[str, Any]:
    if problem is None and ctx.host.dry:
        return _ending(run, True, "The dry run finished. Nothing changed.",
                       "Turn Dry run off, then press Install to make these changes.", [DONE])  # fmt: skip
    if problem is None:
        found = address(host)
        return _ending(run, True, f"The factory at {ctx.sha[:12]} is installed.",
                       "Open the dashboard, then start the orchestrator from its Agents page.", [DONE],
                       links=[found] if found else [])  # fmt: skip
    if install.rolls_back(ctx, keep=keep):
        then = "Everything it made was taken back, so the machine is as it was. Fix what it said, then install again."
    elif keep and ctx.first and not ctx.host.dry and ctx.reached >= install.FIRST_CHANGE:
        then = "What it made is left in place, as you asked. Fix what it said, then install again."
    else:
        then = "Fix what it said, then install again."
    drills = [_action("logs", "Open the drill sessions' output", "")] if run.folder else []
    return _ending(run, False, _stopped(run, "The install stopped"), then, [*drills, DONE], detail=problem)


def uninstall_ending(run: Run, problem: str | None) -> dict[str, Any]:
    if run.kind == "look" and problem is None:
        remove = _action("uninstall", "Remove everything", "danger")
        headline = "This is what Uninstall removes. Nothing changed yet."
        return _ending(run, True, headline, uninstall.LEFT_FOR_YOU, [remove, _action("dismiss", "Cancel", "")])
    if problem is None:
        return _ending(run, True, "The factory is removed from this computer.", uninstall.LEFT_FOR_YOU, [DONE])
    where = "Uninstall would stop" if run.kind == "look" else "The uninstall stopped"
    return _ending(run, False, _stopped(run, where), "Fix what it said, then uninstall again.", [DONE], detail=problem)


def broken(run: Run, exc: Exception) -> dict[str, Any]:
    """The console itself failed: the log and the terminal it runs in have the details."""
    headline = f"The console failed while it ran the {run.kind} ({type(exc).__name__})."
    return _ending(run, False, headline, "Start the console again; if it fails the same way, the log says why.", [DONE])
