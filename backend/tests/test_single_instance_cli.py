from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORSIA_SCRIPT = REPO_ROOT / "scripts" / "corsia"
DEV_SCRIPT = REPO_ROOT / "scripts" / "corsia.d" / "dev.sh"


def _cli_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    for command in ("curl", "launchctl"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nprintf 'test-token\\n'\n")
    fake_python.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CORSIA_PYTHON": str(fake_python),
        "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
        "LAUNCH_AGENTS_DIR": str(tmp_path / "agents"),
    }


def _run_cli(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CORSIA_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=_cli_env(tmp_path),
        timeout=30,
    )


def test_global_cli_has_no_dev_command_or_help(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "help")
    output = result.stdout + result.stderr

    assert "corsia dev" not in output
    assert "prod|dev" not in output
    assert "[prod]" not in output
    assert "Dev checkout backend" not in output
    assert ":3001" not in output


def test_dev_service_cannot_be_installed_or_managed(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "service", "install", "dev")

    assert result.returncode != 0
    assert not (tmp_path / "agents" / "com.corsia.dev.plist").exists()


def test_service_status_reports_only_the_canonical_instance(tmp_path: Path, monkeypatch) -> None:
    # This case exercises launchd, even when the suite runs on Linux. An
    # exported Bash function also works when pytest's temp directory is noexec.
    monkeypatch.setenv("BASH_FUNC_uname%%", "() { printf 'Darwin\\n'; }")
    result = _run_cli(tmp_path, "service", "status")
    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "com.corsia.prod" in output
    assert "com.corsia.dev" not in output
    assert ":3001" not in output


def test_standalone_dev_lifecycle_script_is_removed() -> None:
    assert not DEV_SCRIPT.exists()
