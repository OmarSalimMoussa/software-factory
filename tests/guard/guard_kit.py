"""A temporary factory for the guard's tests: the warden's public files, a product checkout, transcripts, and the
one seam every test goes through, a hook input in and a decision out."""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.guard import decide
from runtime.guard.facts import Decision, Refusal
from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.paths import Places

REPO_ROOT = Path(__file__).resolve().parents[2]
# The product's settings, as the fixture product sets them: they name the hook scripts the guard protects.
FACTORY_TOML = REPO_ROOT / "testdata" / "fixture-product" / "factory.toml"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SID = "0f6c2a3e-1111-4222-8333-944455556666"
TESTER_ID = "a1234567890abc"
REQUIRED = {
    "repo.github": "owner/product",
    "repo.default_branch": "main",
    "repo.protected_paths": ["scripts/deploy.sh", "deploy/"],
}


@dataclass
class Kit:
    root: Path
    places: Places = field(init=False)
    home: Path = field(init=False)
    repo: Path = field(init=False)
    worktree: Path = field(init=False)
    session_transcript: Path = field(init=False)

    def __post_init__(self) -> None:
        self.places = Places.under(self.root)
        self.home = self.root / "home"
        self.repo = self.root / "product"
        self.worktree = self.repo / ".worktrees" / "impl-a"
        self.session_transcript = self.root / "transcripts" / f"{SID}.jsonl"
        for folder in (self.home, self.worktree, self.session_transcript.parent):
            folder.mkdir(parents=True, exist_ok=True)
        (self.repo / "factory.toml").write_bytes(FACTORY_TOML.read_bytes())
        self.places.plugin.mkdir(parents=True)
        (self.places.plugin / "self-reflect.md").write_text("Write to SELF_REFLECT.md when a miss got past you.\n")
        self.settings()
        self.transcript([])

    @property
    def public(self) -> Path:
        return self.places.state / "public"

    def settings(self, **changes: Any) -> None:
        defaults = tomllib.loads((REPO_ROOT / "settings.toml").read_text())
        overrides = {**REQUIRED, "repo.path": str(self.repo), **{k.replace("__", "."): v for k, v in changes.items()}}
        built = settings_module.build(defaults, overrides)
        store.write_json(self.public / "settings.json", built.tree())

    def session(
        self,
        role: str,
        *,
        sid: str = SID,
        state: str = "working",
        worktree: Path | None = None,
        agent_id: str = "",
        parent: str = "",
        extensions: dict[str, int] | None = None,
        retired: str = "",
    ) -> None:
        path = self.public / "sessions.json"
        rows = store.read_json_or(path, {})
        rows[sid] = {
            "name": f"{role}-a", "role": role, "family": "impl", "task": "t-0001", "parent": parent,
            "worktree": str(worktree if worktree is not None else self.worktree), "slot": 1, "state": state,
            "agent_id": agent_id, "retired": retired or ("stopped" if state == "retired" else ""),
            "extensions": extensions or {},
        }  # fmt: skip
        store.write_json(path, rows)

    def switch_off(self, *rules: str) -> None:
        store.write_json(self.public / "switches.json", {"off": list(rules)})

    def ready(self, *records: dict[str, Any]) -> None:
        store.write_json(self.public / "ready" / f"{SID}.json", {"records": list(records)})

    def transcript(self, lines: list[str]) -> Path:
        self.session_transcript.write_text("".join(line + "\n" for line in lines))
        return self.session_transcript

    def subagent(self, agent_id: str, lines: list[str]) -> Path:
        path = self.session_transcript.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines))
        return path

    def payload(self, tool: str, tool_input: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "hook_event_name": "PreToolUse",
            "session_id": SID,
            "tool_name": tool,
            "tool_input": tool_input,
            "transcript_path": str(self.session_transcript),
            "cwd": str(self.worktree),
            "tool_use_id": "toolu_now",
            **extra,
        }

    def decide(self, tool: str, tool_input: dict[str, Any], **extra: Any) -> Decision:
        call = decide.resolve(self.payload(tool, tool_input, **extra), self.places, NOW, self.home)
        assert call is not None
        return decide.decide_tool(call)

    def rule(self, tool: str, tool_input: dict[str, Any], **extra: Any) -> str | None:
        decision = self.decide(tool, tool_input, **extra)
        return decision.rule if isinstance(decision, Refusal) else None

    def bash(self, command: str, **extra: Any) -> str | None:
        return self.rule("Bash", {"command": command}, **extra)

    def git_product(self) -> None:
        """The product as a real git checkout on `main`, with the implementer's worktree on branch `impl-a`."""
        self.worktree.rmdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
        subprocess.run([*git, "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("product\n")
        subprocess.run([*git, "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run([*git, "-C", str(self.repo), "commit", "-qm", "start"], check=True)
        subprocess.run([*git, "-C", str(self.repo), "branch", "other"], check=True)
        add = [*git, "-C", str(self.repo), "worktree", "add", "-q", "-b", "impl-a", str(self.worktree)]
        subprocess.run(add, check=True)

    def head(self) -> str:
        done = subprocess.run(["git", "-C", str(self.worktree), "rev-parse", "HEAD"], capture_output=True, text=True)
        return done.stdout.strip()


def entry(when: datetime, role: str, content: list[dict[str, Any]] | str) -> str:
    return json.dumps({"type": role, "timestamp": when.isoformat(), "message": {"role": role, "content": content}})


def agent_use(use_id: str, kind: str, prompt: str = "Scenarios and checks: 1") -> dict[str, Any]:
    return {"type": "tool_use", "id": use_id, "name": "Agent", "input": {"subagent_type": kind, "prompt": prompt}}


def bash_use(use_id: str, command: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": use_id, "name": "Bash", "input": {"command": command}}


def result(use_id: str, text: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": use_id, "content": text}
