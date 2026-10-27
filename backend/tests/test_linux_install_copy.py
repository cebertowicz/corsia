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


@pytest.mark.parametrize("never_local, expected", [(True, False), (False, True)])
def test_install_summary_uses_backend_local_model_availability(monkeypatch, capsys, never_local, expected):
    monkeypatch.setattr(cli_sources, "_read_token", lambda: "test")
    monkeypatch.setattr(cli_sources, "_fetch", lambda *_args, **_kwargs: ([
        {"source": "codex", "name": "Codex", "state": "ready"},
        {"source": "omlx", "name": "Local model", "state": "missing", "never_local": never_local},
    ], None))
    cli_sources.cmd_sources(argparse.Namespace(action=None, install_summary=True))
    output = capsys.readouterr().out
    assert "Codex: signed in" in output
    assert ("Local model: not set up" in output) is expected


def test_linux_protected_folder_does_not_prompt(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr("builtins.input", lambda _prompt: "~/Documents/work")
    assert cli._prompt_workspace_path() == "~/Documents/work"


def test_both_source_formats_hide_backend_marked_sources():
    rows = [
        {"source": "hidden", "name": "Hidden", "state": "ready", "never_local": True},
        {"source": "omlx", "name": "Visible", "state": "missing"},
    ]
    for formatter in (cli_sources.format_sources, cli_sources.format_install_summary):
        output = "\n".join(formatter(rows))
        assert "Hidden" not in output
        assert "Visible" in output
    assert cli_sources.NONE_READY in "\n".join(cli_sources.format_sources(rows))
