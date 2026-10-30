"""The packaged CLI routes commands from its real entry point."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher(tmp_path):
    resources = tmp_path / "CORSIA test.app" / "Contents" / "Resources"
    for directory in ("bin", "scripts", "python/bin"):
        (resources / directory).mkdir(parents=True)
    entry = resources / "bin" / "corsia"
    shutil.copy(ROOT / "desktop/scripts/corsia", entry)
    for target, label in ((resources / "scripts/corsia", "lifecycle"),
                          (resources / "python/bin/python3", "python")):
        target.write_text(f'#!/bin/sh\nprintf "{label}\\n"\nprintf "<%s>\\n" "$@"\nexit 37\n')
        target.chmod(0o755)
    link = tmp_path / "corsia"
    link.symlink_to(entry)
    return link, resources


@pytest.mark.parametrize("args", [["status"], ["logs", "backend"], ["restart"], ["service", "status"], ["service", "install"], ["start", "--open"], ["stop"]])
def test_lifecycle_commands_forward_arguments_and_exit_code(launcher, args):
    entry, _ = launcher
    result = subprocess.run([entry, *args], text=True, capture_output=True)
    assert result.returncode == 37
    assert result.stdout.splitlines() == ["lifecycle", *[f"<{arg}>" for arg in args]]


@pytest.mark.parametrize("verb", ["init", "setup", "bind-host", "remote-address"])
def test_bootstrap_commands_still_reach_python(launcher, verb):
    entry, resources = launcher
    result = subprocess.run([entry, verb], text=True, capture_output=True)
    assert result.returncode == 37
    assert result.stdout.splitlines() == ["python", f"<{resources / 'backend/cli.py'}>", f"<{verb}>"]


@pytest.mark.parametrize("args", [["redeploy"]])
def test_source_only_commands_explain_their_scope(launcher, args):
    entry, _ = launcher
    result = subprocess.run([entry, *args], text=True, capture_output=True)
    assert result.returncode != 0
    assert "Update CORSIA using the app" in result.stderr
