"""The shell reader is the trap: every route round a rule that reviews proved is refused by the rule it bypassed, and
the ordinary commands an agent runs all day still pass."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from guard_kit import Kit

from runtime.guard import factory_calls
from runtime.guard.shell import ShellError, invocations


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.session("implementer")
    return made


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ('claude --bg --agent implementer "work"', "raw-starts"),
        ('sh -c "claude -p hi"', "raw-starts"),
        ("env A=1 nohup claude --resume abc --bg go &", "raw-starts"),
        ("echo $(claude --print hi)", "raw-starts"),
        ('tmux new -d "claude --bg x"', "raw-starts"),
        ("for i in 1; do claude --bg x; done", "raw-starts"),
        ("{ claude --bg x; }", "raw-starts"),
        ("cat <<EOF\n$(claude --bg x)\nEOF", "raw-starts"),
        ("systemd-run --user --scope claude --bg x", "leave-slice"),
        ("systemctl --user stop some.slice", "leave-slice"),
        ("sudo ls", "leave-slice"),
        ("if true; then sudo ls; fi", "leave-slice"),
        ("! sudo ls", "leave-slice"),
        ("bash <<'EOF'\nsudo ls\nEOF", "leave-slice"),
        ("function f { sudo ls; }; f", "leave-slice"),
        ("exec -a x sudo ls", "leave-slice"),
        ("command -p sudo ls", "leave-slice"),
        ("coproc sudo ls", "leave-slice"),
        ("find . -name x -exec sudo ls {} \\;", "leave-slice"),
        ("echo $(( $(sudo ls | wc -l) + 1 ))", "leave-slice"),
        ("echo $((sudo ls); (true))", "leave-slice"),
        ("env -S'sudo ls'", "leave-slice"),
        ("env --split-string='sudo ls'", "leave-slice"),
        ("run0 ls", "leave-slice"),
        ("dbus-send --session --dest=org.freedesktop.systemd1 /x", "leave-slice"),
        ("uv run --extra dev bash -c 'sudo ls'", "leave-slice"),
        ("uv --project . run bash -c 'sudo ls'", "leave-slice"),
        ("docker ps", "leave-slice"),
        ("podman run --rm alpine true", "leave-slice"),
        ("ssh deploy@prod.example", "leave-slice"),
        ("xdg-open http://localhost:8101", "leave-slice"),
        ("gio open notes.md", "leave-slice"),
        ("git commit --no-verify -m x", "git-bypass"),
        ("git commit --no-veri -m x", "git-bypass"),
        ("git commit -anm x", "git-bypass"),
        ("git -c core.hooksPath=/dev/null commit -m x", "git-bypass"),
        ("git -ccore.hooksPath=/dev/null commit -m x", "git-bypass"),
        ("git --exec-path=/tmp/x commit -m x", "git-bypass"),
        ("git -c user.name=x -C /w commit --no-verify -m x", "git-bypass"),
        ("git config core.bare true", "git-bypass"),
        ("git config --add include.path /tmp/x", "git-bypass"),
        ("git -c alias.st='!sh -c x' st", "git-bypass"),
        ("git remote set-url origin /tmp/x", "git-bypass"),
        ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath git commit -m x", "git-bypass"),
        ("env 'GIT_CONFIG_COUNT=1' 'GIT_CONFIG_KEY_0=core.hooksPath' git commit -m x", "git-bypass"),
        ("export GIT_CONFIG_COUNT=1; git commit -m x", "git-bypass"),
        ("set -a; GIT_CONFIG_COUNT=1; git commit -m x", "git-bypass"),
        ("git push origin HEAD:main", "git-bypass"),
        ("git -C . push origin HEAD:main", "git-bypass"),
        ("git --attr-source HEAD push origin HEAD:main", "git-bypass"),
        ("git push --no-verify origin HEAD:main", "git-bypass"),
        ("while true; do git push origin HEAD:main; done", "git-bypass"),
        ("git send-pack origin HEAD:main", "git-bypass"),
        ("/usr/lib/git-core/git-send-pack origin HEAD:main", "git-bypass"),
        ("git http-push https://x/repo HEAD:main", "git-bypass"),
        ("uv run git commit --no-verify -m x", "git-bypass"),
        ("uv -q run git commit --no-verify -m x", "git-bypass"),
        ("uv run --cache-dir /tmp/c git commit --no-verify -m x", "git-bypass"),
        ("gh api -X PUT repos/o/r/contents/x", "git-bypass"),
        ("gh api repos/o/r/git/refs -f sha=abc", "git-bypass"),
        ("gh pr merge 1", "git-bypass"),
        ("python3 -m checks.runner.passes record", "git-bypass"),
        # A `<<` inside quotes is text, and a delimiter is unquoted as bash does, so no line hides as a body.
        ("git log --grep='<<EOF'\ngit commit --no-verify -m x\nEOF", "git-bypass"),
        ('cat <<E"OF" > x\nhi\nEOF\ngit commit --no-verify -m x', "git-bypass"),
        ("cat <<E-OF > x\nhi\nE-OF\ngit commit --no-verify -m x", "git-bypass"),
        ("cat > GOAL.md <<EOF\n{}\nEOF", "protected-paths"),
        ("sed -i s/a/b/ deploy/promote", "protected-paths"),
        ("rm -rf goal/pictures", "protected-paths"),
        ("sed -i s/x/y/ .github/workflows/ci.yml", "protected-paths"),
        ("echo '[core]' >> .git/config", "protected-paths"),
        ("cp /tmp/h .git/hooks/pre-commit", "protected-paths"),
        ("sed --in-place s/a/b/ GOAL.md", "protected-paths"),
        ("perl -pi -e s/a/b/ GOAL.md", "protected-paths"),
        ("perl -i -le s/a/b/ GOAL.md", "protected-paths"),
        ("sed -i -es/a/b/ -f x.sed GOAL.md", "protected-paths"),
        ("sed -ie s/a/b/ GOAL.md", "protected-paths"),
        ("sed -i s/a/b/ .github//workflows/x.yml", "protected-paths"),
        ("sed -i s/a/b/ .github/./workflows/x.yml", "protected-paths"),
        ("rm -rf deploy/", "protected-paths"),
        ("rm -rf deploy", "protected-paths"),
        ("rm -rf deploy//prod/", "protected-paths"),
        ("rm -rf scripts", "protected-paths"),
        ("rm -rf .git/hooks", "protected-paths"),
        ("mv deploy /tmp/x", "protected-paths"),
        ("cp x deploy/", "protected-paths"),
        ("mv x goal/", "protected-paths"),
        ("uv run sed -i s/a/b/ scripts/deploy.sh", "protected-paths"),
        ("dd if=/dev/zero of=check-values/surfaces.toml", "protected-paths"),
        ("echo x > ~/.gitconfig", "protected-paths"),
        ("echo 'sudo ls' | bash", "unreadable-command"),
        ("bash <<< 'sudo ls'", "unreadable-command"),
        ("cat <<'EOF' > x\nhi\nEOF\ncat x | bash", "unreadable-command"),
        ("bash /dev/stdin < x", "unreadable-command"),
        ("cat <<EOF > x\nnever closed", "unreadable-command"),
        ("true sh <<'EOF'\nEOF\necho 'sudo ls' | bash", "unreadable-command"),
        ("echo 'sudo ls' | bash -s x", "unreadable-command"),
        ("echo 'sudo ls' | bash -o pipefail", "unreadable-command"),
        ("echo 'sudo ls' | busybox sh", "unreadable-command"),
        ("factory $verb t-1", "unreadable-command"),
        ("factory start implementer t-0002", "factory-verbs"),
        ("factory extend impl-b qa", "factory-verbs"),
    ],
)
def test_an_implementers_command_is_refused_by_its_rule(kit: Kit, command: str, rule: str) -> None:
    assert kit.bash(command) == rule


def test_a_hand_run_of_the_installed_checks_is_refused(kit: Kit) -> None:
    runner = kit.places.install / "checks" / "runner" / "passes.py"
    assert kit.bash(f"python3 {runner} record") == "git-bypass"


@pytest.mark.parametrize(
    "command",
    [
        "claude agents --json --all",
        "git status && git log --oneline -3",
        'python3 tools/note.py x "claude --bg is only text here"',
        "cat > notes.md <<'EOF'\nclaude --bg evil is text\nEOF",
        "cat <<'EOF' > notes.md\nsudo is only text here\nEOF",
        "cat <<'EOF' > notes.md\n$(claude --bg is text under a quoted delimiter)\nEOF",
        "cat <<'EOF' | python3 -\nprint(1)\nEOF",
        "uv run --extra dev pytest tests -q",
        "factory qa-ready abc1234 --slices abc1234",
        "factory reflect --kind miss 'slice 2 was stuck'",
        "factory slot claim",
        "factory --help",
        "factory start --help",
        "git config --get user.name",
        "git config user.email",
        "git config --get core.hooksPath",
        "git log -S core.hooksPath",
        "git rebase main",
        "git commit -m 'mention GIT_CONFIG_COUNT=1 in the message'",
        "git commit -m -n",
        "git fetch origin",
        "git reset --hard HEAD~1",
        "git log --oneline -3 # <<EOF is a comment",
        "ls 2>/dev/null; command -v uv",
        "bash -lc 'ls'",
        "bash tools/build.sh list",
        "rm -rf /tmp/build scripts/tmp-x",
        "cp checks/notes.md /tmp/x",
        "gh run list --branch main --limit 1",
        "gh api repos/o/r/actions/runs",
        "grep -n main runtime/cli/main.py",
        "grep -rn GOAL.md docs",
        "echo x > src/goal_test.py",
    ],
)
def test_an_implementers_ordinary_command_passes(kit: Kit, command: str) -> None:
    assert kit.bash(command) is None


@pytest.mark.parametrize(
    "command",
    [
        # Places bash reads `<<` as no heredoc: arithmetic, an expansion's pattern, and a comment after `)`.
        "(( y = 1 <<3 ))\ngit commit --no-verify -m x\n3",
        "for (( i=0; i<<1; i++ )); do :; done\ngit commit --no-verify -m x\n1;",
        "x=abc; echo ${x#<<EOF}\ngit commit --no-verify -m x\nEOF}",
        "echo $[1<<2]\ngit commit --no-verify -m x\n2]",
        "(true)#<<EOF\ngit commit --no-verify -m x\nEOF",
        # A heredoc fed to a shell from a leading redirect or a group, beside a decoy to a shell that runs `-c`.
        "<<'A' bash\ngit commit --no-verify -m x\nA",
        "<<'A' bash\ngit commit --no-verify -m x\nA\nbash -c true <<'B'\nB",
        "{ bash; } <<'A'\ngit commit --no-verify -m x\nA\nbash -c true <<'B'\nB",
        # What only runs once expanded: a variable, a command's output, an alias, a sourced pipe.
        "X=git; $X commit --no-verify -m x",
        "$SHELL -c 'git commit --no-verify -m x'",
        "\"$BASH\" -c 'sudo ls'",
        "$(command -v bash) <<'A'\ngit commit --no-verify -m x\nA",
        "git commit --no-$(echo verify) -m x",
        "git commit --no-${X:-verify} -m x",
        "a=--no-verify; git commit $a -m x",
        "git commit $(echo --no-verify) -m x",
        "echo git commit --no-verify -m x | source /dev/stdin",
        "source <(echo git commit --no-verify -m x)",
        ". <(echo sudo ls)",
        "bash <(echo sudo ls)",
        "alias g=git; g commit --no-verify -m x",
    ],
)
def test_a_line_hidden_from_the_guard_or_named_only_once_expanded_is_refused(kit: Kit, command: str) -> None:
    assert kit.bash(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m \"$(cat <<'EOF'\nfix: a $HOME path\nEOF\n)\"",
        'git commit -m "$msg"',
        'cd "$(git rev-parse --show-toplevel)" && ls',
        'for f in a b; do echo "$f"; done',
        "echo ${HOME}/x $((1<<3))",
        # Arithmetic runs no program of its own, though it starts with a value only the shell knows.
        "start=$(date +%s); until [ $(( $(date +%s) - start )) -ge 30 ]; do sleep 1; done",
        "echo $(( $n + 1 ))",
        "source scripts/lib/slots.sh && slot_number 1",
        'git log --oneline "$base"..HEAD',
        "git diff $(git merge-base main HEAD)",
        'git show "$sha"',
        'git grep -n "$pat"',
        'git add "$f"',
        'gh run view "$id"',
        'git log --author="$me" -3',
        '"$PWD"/tools/build.sh list',
    ],
)
def test_an_ordinary_command_with_expansions_passes(kit: Kit, command: str) -> None:
    assert kit.bash(command) is None


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("git cherry-pick abc1234", True),
        ("git rebase main", True),
        ("git pull", True),
        ("git am fix.patch", True),
        ("git reset --hard abc1234", True),
        ("git reset abc1234", True),
        ("git reset origin/main", True),
        ("git reset --keep abc1234", True),
        ("git reset --soft abc1234", True),
        ("git checkout -B main abc1234", True),
        ("git switch -C main abc1234", True),
        ("git branch -M main", True),
        ("git branch -f main abc1234", True),
        ("git fetch -u . x:main", True),
        ("git clone . /tmp/c", True),
        ("git merge impl-x", True),
        ("git revert -m 1 abc1234", False),
        ("git merge --no-ff impl-x -m m", False),
        ("git merge --abort", False),
        ("git cherry-pick --abort", False),
        ("git rebase --abort", False),
        ("git reset", False),
        ("git reset HEAD", False),
        ("git reset -- file.txt", False),
        ("git reset --hard @", False),
    ],
)
@pytest.mark.parametrize("caller", [{}, {"agent_id": "a1234567890abc", "agent_type": "intake"}], ids=["it", "intake"])
def test_the_orchestrator_moves_the_default_branch_only_by_merge_and_revert(
    tmp_path: Path, command: str, refused: bool, caller: dict[str, str]
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert (kit.bash(command, cwd=str(kit.repo), **caller) == "git-bypass") is refused


@pytest.mark.parametrize(
    "command",
    [
        "rsync -a /tmp/x/ goal/",
        "cp -r /tmp/pictures goal/",
        'git commit "$a" -m x',
        'git push "$a" origin abc1234:main',
        'git checkout "$b"',
    ],
)
def test_a_tree_copied_over_protected_files_or_an_expanded_word_that_moves_things_is_refused(
    tmp_path: Path, command: str
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.bash(command, cwd=str(kit.repo)) is not None


@pytest.mark.parametrize("command", ["find . -name x -delete", "rg --pre sudo x ."])
def test_a_command_that_writes_or_runs_programs_is_no_way_out(tmp_path: Path, command: str) -> None:
    assert Kit(tmp_path).bash(command) == "unpermitted"


@pytest.mark.parametrize(
    ("command", "programs"),
    [
        ("timeout 5 setsid -f env X=1 bash -c 'echo $(git log)'", {"echo", "git"}),
        # Brackets inside quotes within a substitution are text, not the substitution's end.
        ("for p in a b; do echo \"$(grep -o 'bar([^)]*)' $p.js | tr '\\n' ' ')\"; done", {"echo", "grep", "tr"}),
        ('f=$(grep -rln "def get(capture_dir" utilities | head -1); echo $f', {"grep", "head", "echo"}),
        ('echo "$(git log --format="%h (%s)")"', {"echo", "git"}),
    ],
)
def test_the_shell_reader_finds_programs_behind_wrappers_and_quotes(command: str, programs: set[str]) -> None:
    assert programs <= {inv.program for inv in invocations(command)}


def test_the_shell_reader_refuses_a_substitution_left_open() -> None:
    with pytest.raises(ShellError):
        invocations('echo "$(grep x file"')


@pytest.mark.parametrize(
    ("command", "verb"),
    [("factory cause --tried 'the cache, but a cold start loops too'", "cause"),
     ("factory queue task show t-0001", "task.show"), ("factory qa finding --head abc --severity major", "qa.finding")],
)  # fmt: skip
def test_each_new_verb_reads_as_the_warden_names_it(command: str, verb: str) -> None:
    assert factory_calls.verb_of(shlex.split(command)) == verb
