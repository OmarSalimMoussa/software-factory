#!/bin/bash
# bash verify.sh <factory worktree>: lint, format, types, every test folder, comment density and the browser drives,
# one line each, and a non-zero exit if any step failed. The self-check runs apart, on a committed clone.
set -euo pipefail
W=$1
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$W"
U=(uv run -q --python 3.12 --extra dev)
failed=0

# One step's last line of output; a step that fails marks the run failed, and the next step still runs.
step() {
  local name=$1 out
  shift
  if out=$("$@" 2>&1); then echo "$name ${out##*$'\n'}"; else echo "$name FAILED: ${out##*$'\n'}" && failed=1; fi
}

step lint "${U[@]}" ruff check .
step format "${U[@]}" ruff format --check .
step types "${U[@]}" mypy
for d in tests/*/; do
  case $d in tests/__pycache__/ | tests/dashboard_ui/ | tests/self_check/) continue ;; esac
  step "$d" timeout 900 "${U[@]}" pytest -p no:cacheprovider -o faulthandler_timeout=300 "$d"
done
step "density runtime" python3 "$HERE/density.py" "$W" WORK runtime
step "density tests" python3 "$HERE/density.py" "$W" WORK tests
[ -d node_modules ] || npm ci --silent
step drives node tests/dashboard_ui/run.cjs
exit $failed
