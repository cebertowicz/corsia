"""``infra.thread_driver`` never imports ``infra.thread_recovery`` (#2477 R3).

Recovery calls into the driver, so a driver import of recovery is a cycle.
Callers import ``recover_threads`` and ``ambiguous_stale_writes`` from
``infra.thread_recovery`` itself; the driver carries no compatibility export.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RECOVERY = "infra.thread_recovery"


def _imported_modules(tree: ast.AST) -> list[str]:
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.append(node.module)
            modules += [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
    return modules


def test_thread_driver_source_never_imports_thread_recovery() -> None:
    """No import statement in the driver, at load time or in a function, names recovery."""
    tree = ast.parse((BACKEND / "infra" / "thread_driver.py").read_text())
    assert RECOVERY not in _imported_modules(tree)


def test_loading_thread_driver_does_not_load_thread_recovery(tmp_path) -> None:
    """A fresh interpreter that imports the driver has not loaded recovery, even transitively."""
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {
        **os.environ,
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "CORSIA_TEST_MODE": "1",
    }
    probe = f"import sys, infra.thread_driver; print({RECOVERY!r} in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
