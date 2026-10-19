"""A product's checkout as a real git repo, for what reads its default branch: master with standing scenarios, commits
on master itself and --no-ff merges of task branches. The queue folder stays out of git, as on the box."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.qa.scenarios import FOLDER

STANDING = "protects: web/\nneeds: nothing\nruns: in-turn"


def run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


class ProductGit:
    def __init__(self, repo: Path, scenarios: dict[str, str]) -> None:
        self.repo, self.n = repo, 0
        repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "master")
        (repo / ".git" / "info" / "exclude").write_text("/queue/\n")
        (repo / FOLDER).mkdir(parents=True)
        (repo / FOLDER / "README.md").write_text("# Scenarios\n")
        for name, labels in scenarios.items():
            text = f"---\n{labels}\n---\n# {name}\n\n1. Open it.\n   - Expect: it opens.\n"
            (repo / FOLDER / f"{name}.md").write_text(text)
        self.base = self.commit("README.md")

    def git(self, *args: str) -> str:
        who = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
        return subprocess.run(["git", "-C", str(self.repo), *who, *args], capture_output=True, text=True,
                              check=True).stdout.strip()  # fmt: skip

    def _change(self, rel: str) -> None:
        self.n += 1
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(f"change {self.n}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {self.n}")

    def commit(self, rel: str) -> str:
        """A commit on master itself, such as a revert."""
        self._change(rel)
        return self.git("rev-parse", "HEAD")

    def branch(self, rel: str) -> str:
        """A branch off master changing ``rel``, not merged."""
        branch = f"impl-{self.n + 1}"
        self.git("checkout", "-q", "-b", branch)
        self._change(rel)
        self.git("checkout", "-q", "master")
        return branch

    def merge(self, rel: str) -> tuple[str, str]:
        """A branch changing ``rel``, merged into master with --no-ff: the merge commit and the branch."""
        branch = self.branch(rel)
        self.git("merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}")
        return self.git("rev-parse", "HEAD"), branch
