"""Linux setup copy and the unchanged macOS summary."""

import argparse

import pytest

import cli
import cli_sources


@pytest.mark.parametrize("platform, expected", [("linux", False), ("darwin", True)])
def test_workspace_prompt_names_macos_permissions_only_on_macos(monkeypatch, capsys, platform, expected):
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr("builtins.input", lambda _prompt: "/tmp/corsia-work")
    monkeypatch.setattr(cli, "_is_protected_workspace_path", lambda _path: False)
    cli._prompt_workspace_path()
    assert ("macOS TCC" in capsys.readouterr().out) is expected


@pytest.mark.parametrize("platform, expected", [("linux", False), ("darwin", True)])
def test_install_summary_omits_local_model_only_on_linux(monkeypatch, capsys, platform, expected):
    monkeypatch.setattr(cli_sources.sys, "platform", platform)
    monkeypatch.setattr(cli_sources, "_read_token", lambda: "test")
    monkeypatch.setattr(cli_sources, "_fetch", lambda *_args, **_kwargs: ([
        {"source": "codex", "name": "Codex", "state": "ready"},
        {"source": "omlx", "name": "Local model", "state": "missing"},
    ], None))
    cli_sources.cmd_sources(argparse.Namespace(action=None, install_summary=True))
    output = capsys.readouterr().out
    assert "Codex: signed in" in output
    assert ("Local model: not set up" in output) is expected
