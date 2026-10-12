"""Whether the factory's own CI passed on a commit. An install takes only such a commit, so nothing is tested on the
machine at install time. A release file needs no question: CI builds one only after every check passed. For a commit
of the owner's checkout, the installer and the console ask GitHub for its check runs, with the owner's signed-in `gh`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.system import github_keys
from runtime.system.host import Account, Host
from runtime.system.job import Install, github_name, refuse

PASSED, RUNNING, FAILED, UNKNOWN = "passed", "running", "failed", "unknown"
# A job that did not apply ends skipped or neutral; every other conclusion of a finished check is a failure.
_GOOD = frozenset({"success", "skipped", "neutral"})
# One check run to a line, tab-separated: gh prints a jq string as it is, page after page.
CHECK_FIELDS = '.check_runs[] | [.name, .status, (.conclusion // ""), (.html_url // "")] | @tsv'
NO_GH = "the install needs your GitHub CLI signed in to check the factory's CI: run `gh auth login`, then install again"
AGAIN = "Wait for the factory's CI to pass, or fix the commit and push it, then install that commit."


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # queued, in_progress, completed, ...
    conclusion: str  # "" until it completes
    url: str

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.conclusion in _GOOD

    @property
    def what(self) -> str:
        if self.status != "completed":
            return "still running"
        return f"failed ({self.conclusion})"


@dataclass(frozen=True)
class Verdict:
    state: str  # PASSED, RUNNING, FAILED or UNKNOWN
    reason: str  # why no install takes the commit; "" once it passed
    checks: tuple[Check, ...]

    @property
    def open(self) -> tuple[Check, ...]:
        """Each check that failed or still runs."""
        return tuple(check for check in self.checks if not check.ok)

    def refusal(self) -> str:
        """The reason, then one line per open check with its link."""
        lines = [f"  {check.name}: {check.what}: {check.url}" for check in self.open]
        return "\n".join([self.reason, *lines, *([AGAIN] if lines else [])])


def _unknown(reason: str) -> Verdict:
    return Verdict(UNKNOWN, reason, ())


def origin(host: Host, owner: str, checkout: Path) -> str:
    code, out = host.query(["git", "-C", str(checkout), "remote", "get-url", "origin"], user=owner)
    return out.strip() if code == 0 else ""


def verdict(host: Host, owner: Account, factory_origin: str, sha: str) -> Verdict:
    """The factory's CI on ``sha``: passed only when GitHub holds at least one check run for it and every one passed."""
    short = sha[:12]
    repo = github_name(factory_origin)
    if repo is None:
        return _unknown(f"the factory's origin ({factory_origin or 'none'}) is not on GitHub, so its CI cannot be "
                        "checked: an install takes only a commit the factory's CI passed")  # fmt: skip
    gh = github_keys.owner_gh(host, owner)
    if not gh:
        return _unknown(NO_GH)
    asked = [gh, "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100", "--jq", CHECK_FIELDS]
    code, out = host.query(asked, user=owner.name)
    if code != 0:
        return _unknown(f"gh could not read the factory's CI on {short}: {out.strip()[-300:]}")
    return _judged(out, short)


def _judged(out: str, short: str) -> Verdict:
    """The verdict on gh's check runs, one tab-separated line each."""
    checks = []
    for line in filter(str.strip, out.splitlines()):
        fields = line.split("\t")
        if len(fields) != 4:
            return _unknown(f"gh's answer about the factory's CI on {short} cannot be read: {line[:160]}")
        checks.append(Check(*fields))
    found = tuple(checks)
    if not found:
        return _unknown(f"GitHub holds no CI run for {short}: push it and wait for the factory's CI to pass on it")
    if any(check.status == "completed" and not check.ok for check in found):
        return Verdict(FAILED, f"the factory's CI failed on {short}:", found)
    if any(not check.ok for check in found):
        return Verdict(RUNNING, f"the factory's CI is still running on {short}:", found)
    return Verdict(PASSED, "", found)


def passed(ctx: Install) -> None:
    """The install's step: a commit whose CI has not passed is refused before anything changes. A dry run says what it
    would check."""
    host, short = ctx.host, ctx.sha[:12]
    if ctx.release is not None:
        host.say(f"{short} is a release, which CI builds only after every check passed")
        return
    if host.dry:
        repo = github_name(ctx.factory_origin) or "<the factory's GitHub repo>"
        host.say(f"would ask GitHub, with your gh, for every check run of the factory's CI on {short} in {repo}: "
                 "each must have passed")  # fmt: skip
        return
    found = verdict(host, ctx.owner, ctx.factory_origin, ctx.sha)
    if found.state != PASSED:
        raise refuse(found.refusal())
    host.say(f"the factory's CI passed on {short}: {', '.join(check.name for check in found.checks)}")
