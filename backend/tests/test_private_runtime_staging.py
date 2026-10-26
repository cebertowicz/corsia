"""Installer scripts, gh payloads and browser staging stay out of the shared temporary directory."""

from __future__ import annotations

import asyncio
import json
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


@pytest.mark.parametrize("verb, function", [("PATCH", "gh_api_patch_json"), ("POST", "gh_api_post_json")])
def test_gh_payloads_travel_on_stdin_never_in_a_file(verb, function) -> None:
    from reviewer import gh_api

    payload = {"body": "text", "n": 1}
    ok = subprocess.CompletedProcess([], 0, "", "")
    with patch.object(gh_api, "run_gh", return_value=ok) as run:
        assert getattr(gh_api, function)("repos/o/r/x", payload) is True

    args = run.call_args.args[0]
    assert args == ["api", "-X", verb, "repos/o/r/x", "--input", "-"]
    assert json.loads(run.call_args.kwargs["input_text"]) == payload


@pytest.mark.parametrize("function", ["gh_api_patch_json", "gh_api_post_json"])
def test_gh_failures_are_still_logged_and_return_false(function, caplog) -> None:
    from reviewer import gh_api

    failed = subprocess.CompletedProcess([], 1, "", "HTTP 422")
    with patch.object(gh_api, "run_gh", return_value=failed), caplog.at_level("ERROR"):
        assert getattr(gh_api, function)("repos/o/r/x", {}) is False
    assert "failed (rc=1)" in caplog.text and "HTTP 422" in caplog.text


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
    ), patch.object(browser, "_resolve_path", lambda raw, root, **_kw: root / raw):
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


def test_codex_reviewer_output_file_is_private_and_removed_after_the_run(state_root) -> None:
    from reviewer.reviewer_backends import invoke_codex_reviewer

    seen: list[Path] = []

    def run(cmd, **_kwargs):
        output = Path(cmd[cmd.index("--output-last-message") + 1])
        seen.append(output)
        assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
        output.write_text("verdict")
        return subprocess.CompletedProcess(cmd, 0, '{"type":"turn.completed"}\n', "")

    with patch("reviewer.reviewer_backends.subprocess.run", run), patch(
        "reviewer.reviewer_backends.codex_exact_source_controls", return_value=[],
    ), patch("reviewer.reviewer_backends.codex_project_discovery_args", return_value=[]):
        assert invoke_codex_reviewer("p", str(state_root), model="m", source_git_dir=str(state_root), env={}) == "verdict"

    assert seen[0].parent == state_root / "runtime" / "reviewer-output"
    assert not seen[0].exists()
