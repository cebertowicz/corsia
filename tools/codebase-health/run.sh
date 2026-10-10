#!/usr/bin/env bash
# Usage:
#   tools/codebase-health/run.sh <git-ref> [output-dir]
set -euo pipefail

REF="${1:?usage: run.sh <git-ref> [output-dir]}"
OUT="${2:-/tmp/health-${REF}-out}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WT="${REPO_ROOT}/.worktrees/health-${REF}"
VENV="/tmp/health-venv-${REF}"
PY_ROOTS="backend tools"
FE_SRC="frontend/src"

mkdir -p "$OUT"
echo ">> codebase-health: ref=${REF} out=${OUT}"

if [ ! -d "$WT" ]; then
  git -C "$REPO_ROOT" worktree add "$WT" "$REF"
fi
cd "$WT"
echo "   worktree HEAD: $(git rev-parse --short HEAD)"

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"
"$PIP" install -q --disable-pip-version-check \
  radon==6.0.1 vulture==2.16 bandit==1.9.4 ruff==0.15.17 \
  mypy==2.1.0 grimp==3.14 networkx >/dev/null 2>&1 || true

"$VENV/bin/radon" cc $PY_ROOTS -j > "$OUT/cc.json" 2>/dev/null || true
"$VENV/bin/radon" mi $PY_ROOTS -j > "$OUT/mi.json" 2>/dev/null || true

PYTHONPATH=tools "$PY" "${REPO_ROOT}/tools/codebase-health/metrics.py" \
  --worktree "$WT" --out "$OUT" --venv "$VENV" || true

PYTHONPATH=tools "$PY" -m antipatterns --all --severity warning --repo-root . \
  > "$OUT/antipatterns.txt" 2>&1 || true

echo ">> raw instrument outputs in $OUT"
echo ">> assemble metrics JSON, then: $PY tools/codebase-health/score.py <metrics.json>"
echo ">> coverage + svelte-check are run separately — see METHODOLOGY.md"
