from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_launch_agents_dir(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point service-script tests at a throwaway LaunchAgents directory."""
    agents_dir = tmp_path_factory.mktemp("launch-agents")
    monkeypatch.setenv("LAUNCH_AGENTS_DIR", str(agents_dir))
    return agents_dir


def fake_checkout(root: Path, *, python: Path | None = None) -> Path:
    """Build a disposable checkout whose lifecycle script operates only itself.

    ``scripts/corsia`` resolves its own checkout from the physical script
    location, so a test that wants the script to address ``root`` needs a real
    copy of ``scripts/`` there (a symlink would resolve back to the repository).
    The backend is a symlink to the repository backend and ``.venv/bin/python``
    points at ``python`` (this interpreter by default) so identity resolution
    through ``infra.config`` works unchanged.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "scripts", root / "scripts", symlinks=True)
    backend = root / "backend"
    if not backend.exists():
        backend.symlink_to(REPO_ROOT / "backend", target_is_directory=True)
    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    if not interpreter.exists():
        interpreter.symlink_to(python or Path(sys.executable))
    return root


def run_bash(script: str, *, env: dict[str, str] | None = None):
    """Run one service-script test against the repository checkout."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )
