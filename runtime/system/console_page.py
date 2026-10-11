"""What the install console's page says beside a run: whether the factory is installed and answers, what the
install form offers, and how each action ended. Every sentence the page draws is made here or in the run."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.dashboard.client import Warden, WardenDown
from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.system import install, layout, uninstall
from runtime.system.console_run import FAILED, Run
from runtime.system.host import Account, Host
from runtime.system.job import Install

# Loopback only: a proxy from the environment must never carry these probes off the machine.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    if not isinstance(record, dict) or not isinstance(record.get("commit"), str):
        raise ValueError("it names no commit")
    drills = record.get("drills")
    if not isinstance(drills, list) or not all(
        isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("ok"), bool)
        and isinstance(d.get("evidence"), str) for d in drills
    ):  # fmt: skip
        raise ValueError("its drills are not a list of verdicts")
    return record


# ---- status ------------------------------------------------------------------------------------------------------


def status(places: Places, host: Host, owner: Account, factory: Path) -> dict[str, Any]:
    path = layout.install_record(places.state)
    blank: dict[str, Any] = {"installed": False, "headline": "Not installed", "tone": "", "facts": [], "drills": None,
                             "services": [], "address": None, "problem": ""}  # fmt: skip
    try:
        record = read_record(places)
    except (OSError, ValueError) as exc:
        return {**blank, "installed": True, "headline": "Installed", "tone": "warn",
                "problem": f"The install record {path} cannot be read ({exc}): install again."}  # fmt: skip
    if record is None:
        return blank
    sha = record["commit"]
    code, subject = host.query(["git", "-C", str(factory), "log", "-1", "--format=%s", sha, "--"], user=owner.name)
    when = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [
        {"label": "Commit", "value": sha[:12], "sub": subject.strip() if code == 0 else "Not in your checkout"},
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


def _services(places: Places) -> tuple[list[dict[str, Any]], str]:
    try:
        settings = settings_module.build(store.read_json(places.state / "public" / "settings.json"), {})
    # telemetry: surfaced — the status draws why nothing was checked
    except (OSError, store.StoreError, settings_module.SettingsError) as exc:
        why = f"The warden's published settings cannot be read ({type(exc).__name__})"
        return [], f"{why}, so the warden, the dashboard and the shared services were not checked."
    timeout = float(settings.get_int("watch.http_timeout_seconds"))
    try:
        Warden(places.owner_socket, lambda: float(settings.get_int("dashboard.warden_seconds"))).call(
            "snapshot", {"want": ["factory"]}
        )
        warden = True
    # telemetry: surfaced — the status draws it as the warden not answering
    except WardenDown:
        warden = False
    port = settings.get_int("dashboard.port")
    found = {
        "The warden": warden,
        "The dashboard": _answers(f"http://127.0.0.1:{port}/", timeout, any_status=True),
        "GlitchTip": _answers(layout.GLITCHTIP_HEALTH, timeout, any_status=False),
        "Langfuse": _answers(layout.LANGFUSE_HEALTH, timeout, any_status=False),
    }
    return [{"name": name, "ok": ok, "text": "Answers" if ok else "Does not answer"} for name, ok in found.items()], ""


def _answers(url: str, timeout: float, *, any_status: bool) -> bool:
    try:
        with _DIRECT.open(url, timeout=timeout) as reply:
            return any_status or reply.status == 200
    # telemetry: surfaced — the status draws each service that does not answer
    except urllib.error.HTTPError:
        return any_status
    except (OSError, ValueError):
        return False


def address(host: Host) -> dict[str, str] | None:
    """The dashboard on the tailnet, where `tailscale serve` publishes it; None when tailscale names no machine."""
    code, out = host.query(["tailscale", "status", "--json"])
    try:
        name = json.loads(out)["Self"]["DNSName"] if code == 0 else ""
    # telemetry: surfaced — the status draws the address as unknown
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(name, str) or not name.strip("."):
        return None
    url = f"https://{name.strip('.')}/"
    return {"label": "Open the dashboard", "url": url, "text": url}


# ---- the install form --------------------------------------------------------------------------------------------


def install_form(host: Host, owner: Account, factory: Path, commit: str | None, repo: Path | None,
                 record: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    # --repo, else the checkout the last install named; with neither, the field is empty for the owner to fill.
    named = record.get("product") if record else None
    product = str(repo) if repo else named if isinstance(named, str) else ""
    verb = "Reinstall" if record else "Install"
    return {
        "heading": verb,
        "commit": {"label": "Factory commit", **_commit(host, owner, factory, commit)},
        "other": {"label": "Another commit", "placeholder": "A sha, or a branch of your factory checkout"},
        "repo": {"label": "Product checkout", "value": product,
                 "hint": "Your own checkout of the product the factory works on."},  # fmt: skip
        "healthchecks": {"label": "Create the Healthchecks.io checks",
                         "hint": "They tell you when the warden or the orchestrator goes quiet. Asks for an API key."},
        "keep": {"label": "Keep a failed first install",
                 "hint": "Leave what it made in place to look at, instead of taking it back."},
        "dry": {"label": "Dry run", "hint": "Run every step, reading this machine and changing nothing."},
        "button": verb,
        "dry_button": "Dry run",
    }  # fmt: skip


def _commit(host: Host, owner: Account, factory: Path, given: str | None) -> dict[str, Any]:
    git = ["git", "-C", str(factory)]
    if given:
        ref, note = given, "The commit you named with --commit"
    else:
        code, out = host.query([*git, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], user=owner.name)
        branch = out.strip().removeprefix("origin/")
        if code != 0 or not branch:
            problem = f"{factory} names no default branch: run `git remote set-head origin --auto` there, or name "
            return {"value": "", "short": "", "subject": "", "note": "", "problem": problem + "another commit."}
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
        step = next((row.title for row in run.rows if row.state == FAILED and not row.group), None)
    return f"{what} at “{step}”." if step else f"{what} before its first step."


def _detail(problem: str) -> str:
    return problem[:1].upper() + problem[1:]


def _ending(ok: bool, headline: str, then: str, actions: list[dict[str, str]], *, detail: str = "",
            folder: Path | None = None, links: list[dict[str, str]] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"ok": ok, "headline": headline, "detail": detail, "next": then, "folder": str(folder) if folder else None,
            "links": links or [], "actions": actions}  # fmt: skip


DONE = _action("dismiss", "Done", "")


def install_ending(ctx: Install, problem: str | None, keep: bool, run: Run, host: Host) -> dict[str, Any]:
    if problem is None and ctx.host.dry:
        return _ending(True, "The dry run finished. Nothing changed.",
                       "Turn Dry run off, then press Install to make these changes.", [DONE])  # fmt: skip
    if problem is None:
        found = address(host)
        return _ending(True, f"The factory at {ctx.sha[:12]} is installed.",
                       "Open the dashboard, then start the orchestrator from its Agents page.", [DONE],
                       links=[found] if found else [])  # fmt: skip
    if install.rolls_back(ctx, keep=keep):
        then = "Everything it made was taken back, so the machine is as it was. Fix what it said, then install again."
    elif keep and ctx.first and not ctx.host.dry and ctx.reached >= install.FIRST_CHANGE:
        then = "What it made is left in place, as you asked. Fix what it said, then install again."
    else:
        then = "Fix what it said, then install again."
    logs = [_action("logs", "Open the drill sessions' output", "")] if run.folder else []
    return _ending(False, _stopped(run, "The install stopped"), then, [*logs, DONE],
                   detail=_detail(problem), folder=run.folder)  # fmt: skip


def uninstall_ending(run: Run, problem: str | None) -> dict[str, Any]:
    if run.kind == "look" and problem is None:
        remove = _action("uninstall", "Remove everything", "danger")
        headline = "This is what Uninstall removes. Nothing changed yet."
        return _ending(True, headline, uninstall.LEFT_FOR_YOU, [remove, _action("dismiss", "Cancel", "")])
    if problem is None:
        return _ending(True, "The factory is removed from this machine.", uninstall.LEFT_FOR_YOU, [DONE])
    where = "Uninstall would stop" if run.kind == "look" else "The uninstall stopped"
    return _ending(False, _stopped(run, where), "Fix what it said, then uninstall again.", [DONE],
                   detail=_detail(problem))  # fmt: skip


def broken(kind: str, exc: Exception) -> dict[str, Any]:
    """The console itself failed: the terminal it runs in has the details."""
    headline = f"The console failed while it ran the {kind} ({type(exc).__name__})."
    return _ending(False, headline, "Its terminal shows what went wrong.", [DONE])
