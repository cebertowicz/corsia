"""Contracts for exercising the checked-out worktree in local runners."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _runner(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_e2e_runner_uses_shared_venv_but_current_worktree_backend():
    script = _runner("e2e-local")

    assert 'VENV_PY="${CANONICAL_REPO_ROOT}/.venv/bin/python"' in script
    assert 'PLAYWRIGHT_ENV+=(CORSIA_E2E_BACKEND_PYTHON="$VENV_PY")' in script
    assert 'PLAYWRIGHT_ENV+=(CORSIA_E2E_STATE_DIR="$STATE_ROOT")' in script
    assert 'cd "${REPO_ROOT}/backend"' in script
    assert 'cd "${CANONICAL_REPO_ROOT}/backend"' not in script


def test_eval_runner_uses_current_worktree_backend_and_scenarios():
    script = _runner("eval-local")

    assert 'VENV_PY="${CANONICAL_REPO_ROOT}/.venv/bin/python"' in script
    assert 'cd "${REPO_ROOT}/backend"' in script
    assert 'cd "$REPO_ROOT"' in script
    assert 'cd "${CANONICAL_REPO_ROOT}/backend"' not in script
    assert 'cd "$CANONICAL_REPO_ROOT"' not in script
