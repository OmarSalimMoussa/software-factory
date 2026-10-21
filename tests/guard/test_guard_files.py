"""What a session may write and ask: edits stay in its own worktree, protected paths and the queue are never written
by hand, a task's status is the orchestrator's, and the factory command runs alone."""

from __future__ import annotations

from pathlib import Path

import pytest
from guard_kit import NOW, TESTER_ID, Kit, entry

from runtime.guard.facts import Refusal, Rewrite

EXPLORE = {"subagent_type": "Explore", "prompt": "p"}


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.session("implementer")
    return made


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("{worktree}/src/app.py", None),
        ("{repo}/.worktrees/impl-b/src/app.py", "own-worktree"),
        ("{repo}/src/app.py", "own-worktree"),
        ("{home}/notes.md", "own-worktree"),
        ("{worktree}/GOAL.md", "protected-paths"),
        ("{worktree}/goal/hero.png", "protected-paths"),
        ("{worktree}/.github/workflows/ci.yml", "protected-paths"),
        ("{worktree}/factory.toml", "protected-paths"),
        ("{worktree}/deploy/promote", "protected-paths"),
        ("{worktree}/check-values/surfaces.toml", "protected-paths"),
        ("{worktree}/.git", "protected-paths"),
        ("{repo}/queue/tasks/t-0001.md", "queue-status"),
        ("{install}/runtime/guard/hook.py", "protected-paths"),
        ("{home}/.claude/settings.json", "protected-paths"),
    ],
)
def test_an_edit_stays_in_the_sessions_own_worktree_and_off_protected_paths(
    kit: Kit, path: str, rule: str | None
) -> None:
    target = path.format(worktree=kit.worktree, repo=kit.repo, home=kit.home, install=kit.places.install)
    assert kit.rule("Write", {"file_path": target, "content": "x"}) == rule


def test_an_edit_through_a_link_out_of_the_worktree_is_refused(kit: Kit) -> None:
    (kit.worktree / "out").symlink_to(kit.home)
    assert kit.rule("Edit", {"file_path": str(kit.worktree / "out" / "x.py")}) == "own-worktree"


def test_the_orchestrators_worktree_is_the_main_checkout(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.rule("Write", {"file_path": str(kit.repo / ".factory" / "body.md"), "content": "x"}) is None
    assert kit.rule("Write", {"file_path": str(kit.repo / "notes.md"), "content": "x"}) == "orchestrator-writes"
    assert kit.rule("Write", {"file_path": str(kit.repo / "GOAL.md"), "content": "x"}) == "protected-paths"
    kit.session("implementer", sid="impl-1")
    assert kit.rule("Write", {"file_path": str(kit.worktree / "app.py"), "content": "x"}) == "own-worktree"


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("echo x > {repo}/queue/tasks/t-0001.md", "queue-status"),
        ("cd {repo} && rm -rf queue", "queue-status"),
        ("factory task status t-0001 building", "queue-status"),
        ("factory queue task new --title x", "factory-verbs"),
        ("factory promote", "factory-verbs"),
        ("factory snapshot", "factory-verbs"),
        ("factory no-such-verb", "factory-verbs"),
        ("factory qa-browser qa-ux", "factory-verbs"),
        ("factory status", None),
        ("factory qa plan", None),
        ("factory slot up web", None),
        ("factory spec save t-0001 {worktree}/spec.md", "factory-verbs"),
    ],
)
def test_the_queue_and_the_factory_command_are_used_only_as_the_role_allows(
    kit: Kit, command: str, rule: str | None
) -> None:
    assert kit.bash(command.format(repo=kit.repo, home=kit.home, worktree=kit.worktree)) == rule


@pytest.mark.parametrize(
    ("role", "command", "kind", "rule"),
    [
        ("orchestrator", "factory task status t-0001 building", "", None),
        ("orchestrator", "factory task status t-0001 building", "intake", "queue-status"),
        ("orchestrator", "factory queue task new --title x", "intake", None),
        ("orchestrator", "factory queue task new --title x", "triage", "factory-verbs"),
        ("orchestrator", "factory triage apply --body plan.json", "triage", "factory-verbs"),
        ("orchestrator", "factory queue check abc1234 --body r.md", "qa-regression", "factory-verbs"),
        ("orchestrator", "factory inbox --wait", "intake", "factory-verbs"),
        ("orchestrator", "factory start implementer t-0001", "", None),
        ("orchestrator", "factory start owner mine", "", "factory-verbs"),
        ("orchestrator", "factory start orchestrator", "", "factory-verbs"),
        ("designer", "factory spec save t-0001 spec.md", "", None),
        ("designer", "factory qa-ready abc1234", "", "factory-verbs"),
        ("designer", "factory errors", "critic-security", "factory-verbs"),
        ("orchestrator", "factory chat r-2026-10-09-0001 --before 7", "triage", None),
        ("orchestrator", "factory chat r-2026-10-09-0001", "qa-regression", "factory-verbs"),
        ("implementer", "factory chat r-2026-10-09-0001", "", "factory-verbs"),
        ("implementer", "factory request new --from qa-backend", "qa-backend", "factory-verbs"),
        ("implementer", "factory errors --where staging", "qa-backend", None),
        ("implementer", "factory check", "qa-backend", "factory-verbs"),
    ],
)
def test_each_role_and_its_subagents_run_only_their_verbs(
    tmp_path: Path, role: str, command: str, kind: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session(role, worktree=kit.repo if role == "orchestrator" else kit.worktree)
    caller = {"agent_id": TESTER_ID, "agent_type": kind} if kind else {}
    if kind.startswith("qa-"):
        kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])  # a briefed tester, in its time
    assert kit.bash(command, cwd=str(kit.repo if role == "orchestrator" else kit.worktree), **caller) == rule


NOT_A_COMMAND = "is not a factory command"


@pytest.mark.parametrize(
    ("command", "said"),
    [
        ("factory queue show t-0057", f"`factory queue show` {NOT_A_COMMAND}; `factory queue --help` lists them"),
        ("factory no-such-verb", f"`factory no-such-verb` {NOT_A_COMMAND}; `factory --help` lists them"),
        ("factory inbox --wait", "`factory inbox.wait` is not the intake subagent's to run; report to your parent "
                                 "instead"),
        ("factory queue task show t-0057", None),
        ("factory queue next", None),
    ],
)  # fmt: skip
def test_intake_reads_the_queue_and_hears_a_command_that_does_not_exist_named_as_such(
    tmp_path: Path, command: str, said: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    decision = kit.decide("Bash", {"command": command}, cwd=str(kit.repo), agent_id=TESTER_ID, agent_type="intake")
    assert (decision.reason if isinstance(decision, Refusal) else None) == said


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("factory status && touch x", None),
        ("factory status; ls", None),
        ("factory status || true", None),
        ("ls; factory status; factory qa plan", None),
        ("factory status | tail -3", "factory-alone"),
        ("ls; factory status | tail -3", "factory-alone"),
        ("factory status &", "factory-alone"),
        ("(factory status)", "factory-alone"),
        ("cd src && factory status", None),
        ("cd /home/software-agent/product; factory status", None),
        ("cd 'my work' && factory status", None),
        ("cd $HOME && factory status", None),
        ("cd a && cd b && factory status", None),
        ("cd src && factory status && ls", None),
        ('ls; echo "$(factory status)"', "factory-alone"),
        ("ls; (factory status)", "factory-alone"),
        ("timeout 60 factory status", "factory-alone"),
        ("/usr/local/bin/factory status", "factory-alone"),
        ("FACTORY_X=1 factory status", "factory-alone"),
        ("bash -c 'factory status'", "factory-alone"),
        ("factory status > out.txt", "factory-alone"),
        ("factory request new --severity normal --where x < body.md", "factory-alone"),
        ("factory request new --severity normal --where x <<'EOF'\nbody\nEOF", "factory-alone"),
        ("factory status\nls", None),
        ("cat >> notes.md <<'EOF'\nfactory status\nEOF\nfactory qa plan", None),
        ("bash <<'EOF'\nfactory status\nEOF\nfactory qa plan", "factory-alone"),
        ('factory reflect --kind miss "$(cat note.md)"', "factory-alone"),
        ('factory reflect --kind miss "$HOME"', "factory-alone"),
        ("factory reflect --kind miss notes/*", "factory-alone"),
        ("factory status --agent-id a1234567890abc", "agent-id"),
        ("factory --agent-id=a1234567890abc status", "agent-id"),
        ("factory reflect --kind miss 'it costs $5, and * is fine in quotes'", None),
        ('factory reflect --kind miss "a * b"', None),
        ("factory qa plan", None),
    ],
)
def test_a_sessions_factory_commands_are_each_plain_and_name_no_subagent(
    kit: Kit, command: str, rule: str | None
) -> None:
    decision = kit.decide("Bash", {"command": command})
    assert (decision.rule if isinstance(decision, Refusal) else decision) == rule


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("cd {worktree} && factory records --help 2>&1 | head -30", "factory-alone"),
        ("factory doing 'Checking how GLM is reached' ; grep -rn -i \"glm\" --include=*.py . 2>/dev/null | head", None),
        ("cd {worktree}/harness && ls step; sed -n 1,80p step/session.py; factory slot --help", None),
        ("git diff --stat main...HEAD -- docs | cat; ls docs/specs | tail -5; factory doing 'Writing the spec'", None),
        ("cat >> docs/a.md <<'EOF'\n\n## Critic rounds\nEOF\nfactory spec save t-0001 docs/a.md --slices 2", None),
    ],
)
def test_a_designers_factory_commands_may_share_its_call_with_others_but_not_hand_their_answer_on(
    tmp_path: Path, command: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("designer")
    assert kit.bash(command.format(worktree=kit.worktree)) == rule


@pytest.mark.parametrize(
    ("command", "cause"),
    [
        ("factory queue task show t-0007 | head -80", "`|` pipes the answer into another command"),
        ("factory status &", "`&` runs it in the background"),
        ("factory status 2>&1", "`>&` joins a second command or a file"),
        ("factory status > out.txt", "`>` sends the answer to a file"),
        ("timeout 60 factory status", "something comes before the bare word factory"),
        ("factory reflect --kind miss 'one\ntwo'", "a line break starts a second command"),
        ('factory reflect --kind miss "costs $5"', "the shell would expand `$` there"),
    ],
)
def test_a_factory_alone_refusal_names_what_made_the_call_more_than_one_command(
    kit: Kit, command: str, cause: str
) -> None:
    decision = kit.decide("Bash", {"command": command})
    # One text for every cause left agents guessing, and they retried the same call until the incident filed.
    assert isinstance(decision, Refusal) and decision.rule == "factory-alone"
    assert decision.reason.startswith(cause) and "give factory a command of its own" in decision.reason


@pytest.mark.parametrize(
    ("tool", "rule"),
    [
        ("Workflow", "banned-tools"),
        ("RemoteTrigger", "banned-tools"),
        ("Artifact", "banned-tools"),
        ("ArtifactComments", "banned-tools"),
        ("mcp__claude_ai_Google_Drive__search", "banned-tools"),
        ("Read", None),
    ],
)
def test_tools_that_run_or_keep_work_outside_the_factory_are_refused(kit: Kit, tool: str, rule: str | None) -> None:
    assert kit.rule(tool, {"file_path": str(kit.worktree / "a.md")}) == rule


ME = f"factory --agent-id {TESTER_ID}"
IN = "slot sign-in qa-x@product.test"
QA = "qa result --head abc1234"
BODY = f"cat > .factory/{TESTER_ID}/f.md <<'EOF'\nfactory {QA}\nEOF\n"


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        (f"date +%s; factory {IN}", f"date +%s; {ME} {IN}"),
        (f"factory {IN}; factory {QA}", f"{ME} {IN}; {ME} {QA}"),
        (f"factory {IN}\nfactory {QA} || ls", f"{ME} {IN}\n{ME} {QA} || ls"),
        (f"cd .factory && factory {QA}; factory {QA}", f"cd .factory && {ME} {QA}; {ME} {QA}"),
        (f"  factory {QA} --blocked 'at (1,2); && quit'", f"  {ME} {QA} --blocked 'at (1,2); && quit'"),
        (f'factory {QA} --blocked "tom\'s box && froze"', f'{ME} {QA} --blocked "tom\'s box && froze"'),
        (f"echo 'it'\"'\"'s; factory {QA}' && factory {QA}", f"echo 'it'\"'\"'s; factory {QA}' && {ME} {QA}"),
        (f'echo "a \\" ; factory {QA}"', f'echo "a \\" ; factory {QA}"'),
        (f"{BODY}factory {QA}", f"{BODY}{ME} {QA}"),
        # bash splits words only at spaces and tabs, so `\u00a0factory` is no factory: neither checked nor named.
        (f"\u00a0factory {QA}; factory {QA}", f"\u00a0factory {QA}; {ME} {QA}"),
        ("\u00a0factory task status t-0001 done", "\u00a0factory task status t-0001 done"),
        (f"date +%s; factory {IN} | grep -o 'http[^ ]*'", "factory-alone"),
        (f"L=$(factory {IN}); echo ok", "factory-alone"),
        (f"factory {QA} > /dev/null; echo done", "factory-alone"),
        (f"for i in 1 2; do factory {QA}; done", "factory-alone"),
        (f"(factory {QA}); ls", "factory-alone"),
        (f"ls & factory {QA}", "factory-alone"),
        (f'ls; factory {QA} --blocked "$WHY"', "factory-alone"),
        (f"ls; FOO=1 factory {QA}", "factory-alone"),
        (f"ls; bash -c 'factory {QA}'", "factory-alone"),
        # The guard reads these two lines as one factory command, which the name would not lead.
        (f"factory {QA} <\nfactory {QA}", "factory-alone"),
        (f"factory {QA}; factory --agent-id a9999999999999 {QA}", "agent-id"),
        (f"ls && factory --agent-id=a9999999999999 {QA}", "agent-id"),
    ],
)
def test_a_subagent_names_itself_in_each_factory_command_of_its_call_and_runs_none_without_it(
    tmp_path: Path, command: str, runs: str
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    decision = kit.decide("Bash", {"command": command}, agent_id=TESTER_ID, agent_type="qa-ui-function")
    if runs in {"factory-alone", "agent-id"}:
        assert isinstance(decision, Refusal) and decision.rule == runs
    else:
        assert isinstance(decision, Rewrite) and decision.updated_input["command"] == runs


READER = {"agent_id": "a1234567890abc", "agent_type": "Explore"}  # a reader: no tester time to count


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("git reset --hard", "subagent-writes"),
        ("git stash", "subagent-writes"),
        ("git checkout -- app.py", "subagent-writes"),
        ("sed -i s/a/b/ app.py", "subagent-writes"),
        ("sed -ni -e s/a/b/p app.py", "subagent-writes"),
        ("sed -i 's/a b$/c/' .factory/notes.md", None),
        ("sed -i -e s/a/b/ --expression=s/c/d/ .factory/notes.md", None),
        ("perl -pi -e s/a/b/ .factory/notes.md", None),
        ("sed -i s/a/b/ /tmp/walk.py", None),
        ("echo x > app.py", "subagent-writes"),
        ("echo x > .factory/notes.md", None),
        ("chmod 700 .factory/notes.md", None),
        ("chmod u+x,go-w app.py", "subagent-writes"),
        ("chmod 700 700", "subagent-writes"),
        ("git log --oneline", None),
        ("git diff main", None),
        ("echo x > /tmp/scratch.txt", None),
    ],
)
def test_a_subagent_writes_only_in_its_sessions_scratch_and_runs_only_git_that_reads(
    tmp_path: Path, command: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    assert kit.bash(command, **READER) == rule


@pytest.mark.parametrize(
    ("path", "rule"), [("app.py", "subagent-writes"), (".factory/brief.md", None)], ids=["product-file", "scratch"]
)
def test_a_subagents_edit_tool_writes_only_in_its_sessions_scratch(tmp_path: Path, path: str, rule: str | None) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    assert kit.rule("Write", {"file_path": str(kit.worktree / path), "content": "x"}, **READER) == rule


@pytest.mark.parametrize(
    ("path", "rule"),
    [(f".factory/{TESTER_ID}/f-1.md", None), (".factory/f-1.md", "subagent-writes"), (".factory/b77/f-1.md",
     "subagent-writes")],
    ids=["its-own-folder", "the-shared-folder", "another-testers-folder"],
)  # fmt: skip
def test_a_tester_writes_only_in_its_own_folder_as_testers_of_one_worktree_run_at_once(
    tmp_path: Path, path: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    asked = {"file_path": str(kit.worktree / path), "content": "Save fails."}
    assert kit.rule("Write", asked, agent_id=TESTER_ID, agent_type="qa-visual") == rule


@pytest.mark.parametrize(
    ("tool", "tool_input", "kind", "folder"),
    [
        ("Write", {"file_path": ".factory/f-1.md", "content": "x"}, "qa-visual", f".factory/{TESTER_ID}"),
        ("Bash", {"command": "rm -f .factory/jar.txt"}, "qa-visual", f".factory/{TESTER_ID}"),
        ("Bash", {"command": "echo x > app.py"}, "Explore", ".factory"),
    ],
    ids=["a-testers-write", "a-testers-shell-write", "a-readers-shell-write"],
)
def test_a_subagents_refused_write_names_the_folder_it_writes_in_and_the_tools_that_do(
    tmp_path: Path, tool: str, tool_input: dict[str, str], kind: str, folder: str
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    asked = {**tool_input, "file_path": str(kit.worktree / tool_input["file_path"])} if tool == "Write" else tool_input
    decision = kit.decide(tool, asked, agent_id=TESTER_ID, agent_type=kind)
    assert isinstance(decision, Refusal) and decision.rule == "subagent-writes"
    assert f"{kit.worktree / folder}/" in decision.reason and "the Write or Edit tool" in decision.reason


@pytest.mark.parametrize("command", ["D=.factory/x; chmod 700 $D", 'echo x > "$OUT"', "touch `pwd`/f"])
def test_a_write_through_a_shell_variable_is_refused_naming_why(tmp_path: Path, command: str) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    decision = kit.decide("Bash", {"command": command}, **READER)
    assert isinstance(decision, Refusal) and decision.rule == "subagent-writes"
    assert "cannot tell where a shell variable points" in decision.reason


@pytest.mark.parametrize(
    ("finishing", "command", "rule"),
    [
        ("", "git commit -m fix", "orchestrator-writes"),
        ("MERGE_HEAD", "git commit --no-edit", None),
        ("REVERT_HEAD", "git commit -m 'Revert abc: x' -m 'This reverts commit abc.'", None),
        ("REVERT_HEAD", "git commit --no-edit", None),
        ("REVERT_HEAD", "git commit -m 'Revert the merge'", "orchestrator-writes"),
        ("", "echo x > app.py", "orchestrator-writes"),
        ("", "echo x > .factory/body.md", None),
    ],
    ids=["plain-commit", "finishing-a-merge", "finishing-a-revert", "a-revert-with-git-s-message",
         "a-revert-naming-nothing", "product-file", "scratch"],
)  # fmt: skip
def test_the_orchestrator_writes_no_product_code_and_commits_only_to_finish_a_merge_or_a_revert(
    tmp_path: Path, finishing: str, command: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    (kit.repo / ".git").mkdir(exist_ok=True)
    if finishing:
        (kit.repo / ".git" / finishing).write_text("abc\n")
    assert kit.bash(command, cwd=str(kit.repo)) == rule


@pytest.mark.parametrize(
    ("role", "tool", "tool_input", "rule"),
    [
        ("implementer", "EnterWorktree", {"name": "elsewhere"}, "worktree-tools"),
        ("orchestrator", "EnterWorktree", {"path": "{repo}/.claude/worktrees/impl-b"}, "worktree-tools"),
        ("implementer", "ExitWorktree", {"action": "keep"}, "worktree-tools"),
        ("implementer", "Agent", {**EXPLORE, "isolation": "worktree"}, "worktree-tools"),
        ("implementer", "Agent", EXPLORE, None),
    ],
    ids=["enter-new", "enter-another", "exit", "isolated-subagent", "plain-subagent"],
)
def test_a_session_never_makes_or_leaves_a_worktree_of_its_own(
    tmp_path: Path, role: str, tool: str, tool_input: dict[str, str], rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session(role, worktree=kit.repo if role == "orchestrator" else None)
    filled = {k: v.format(repo=kit.repo) for k, v in tool_input.items()}
    assert kit.rule(tool, filled) == rule
