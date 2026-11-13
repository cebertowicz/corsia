"""Installer scripts, gh payloads and browser staging stay out of the shared temporary directory."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    from infra import config

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", root)
    return root


def test_installer_script_is_staged_in_a_private_state_directory(state_root, monkeypatch) -> None:
    from api import cli_install

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit):
            return b"#!/bin/sh\nexit 0\n"

    monkeypatch.setattr(cli_install.urllib.request, "urlopen", lambda *_a, **_k: Response())
    path = cli_install._download_script("codex")

    assert path.parent == state_root / "runtime" / "cli-installers"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert subprocess.run(["bash", str(path)], check=False).returncode == 0


def test_browser_screenshot_stages_in_a_private_state_directory(state_root, tmp_path) -> None:
    from tools import browser

    seen: list[Path] = []

    async def fake_run(argv, **_kwargs):
        stage = Path(argv[1])
        seen.append(stage)
        stage.write_bytes(b"png")
        return "ok"

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with patch.object(browser, "_run", fake_run), patch.object(
        browser, "_execution_context", lambda _kwargs: ("thread", "project"),
    ), patch.object(browser, "resolve_path", lambda raw, root, **_kw: root / raw):
        result = asyncio.run(browser.BrowserScreenshotTool(workspace).execute(path="shot.png"))

    assert "Screenshot saved" in result
    assert seen and seen[0].parent.parent == state_root / "runtime" / "browser-staging"
    assert (workspace / "shot.png").read_bytes() == b"png"
    assert not seen[0].parent.exists()


def test_startup_sweep_clears_leftover_installers_and_staged_browser_directories(state_root) -> None:
    from tools.private_runtime_files import (
        BROWSER_STAGING_DIR, CLI_INSTALLER_DIR, REVIEWER_OUTPUT_DIR, private_mkstemp, private_runtime_dir,
        sweep_private_runtime_files,
    )

    descriptor, installer = private_mkstemp(CLI_INSTALLER_DIR, prefix="corsia-cli-install-", suffix=".sh")
    Path(installer).write_text("x")
    leftover_output = private_runtime_dir(REVIEWER_OUTPUT_DIR) / "codex-last-x.txt"
    leftover_output.write_text("x")
    staged = private_runtime_dir(BROWSER_STAGING_DIR) / "corsia-browser-open-abc"
    staged.mkdir()
    (staged / "document.png").write_bytes(b"x")
    outside = state_root / "outside"
    outside.mkdir()
    (private_runtime_dir(BROWSER_STAGING_DIR) / "link").symlink_to(outside)

    os.close(descriptor)
    assert sweep_private_runtime_files() == 3
    assert not leftover_output.exists()
    assert not Path(installer).exists()
    assert not staged.exists()
    assert outside.exists()

