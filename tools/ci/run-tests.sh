#!/usr/bin/env bash
# Run every tests/test_*.py module on its own, the way the handoffs do:
# tests/ is not a package, and three SHARC modules import a sibling by bare
# name, so tests/ goes on PYTHONPATH. Tests that need firmware skip without it.
#
#     tools/ci/run-tests.sh [PYTHON]
set -uo pipefail
python=${1:-python}
cd "$(dirname "$0")/../.."
export PYTHONPATH=tests
failed=()
for t in tests/test_*.py; do
  mod=tests.$(basename "$t" .py)
  if out=$("$python" -m unittest "$mod" 2>&1); then
    printf 'ok    %-45s %s\n' "$mod" "$(printf '%s\n' "$out" | grep -E '^(Ran |OK)' | tr '\n' ' ')"
  else
    printf 'FAIL  %s\n' "$mod"
    printf '%s\n' "$out" | tail -40 | sed 's/^/      /'
    failed+=("$mod")
  fi
done
if ((${#failed[@]})); then
  printf '\n%d module(s) failed: %s\n' "${#failed[@]}" "${failed[*]}"
  exit 1
fi
echo "all test modules passed"
