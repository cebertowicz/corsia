"""CLI contracts for workspace bootstrap creation and immutable roots."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

import pytest


def _platform_paths():
    return importlib.import_module("infra.platform_paths")


def _init_args(
    path: Path | None,
    *,
    force: bool = False,
    name: str | None = None,
):
    return argparse.Namespace(
        force=force,
        path=str(path) if path is not None else None,
        name=name,
        setup="local",
    )


def test_cli_init_keeps_operational_state_out_of_workspace(
    tmp_path,
    monkeypatch,
):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))
    args = _init_args(workspace, name="Primary")

    assert cli.cmd_init(args) == 0

    runtime = paths.load_bootstrap(bootstrap)
    assert runtime.workspace.root == workspace.resolve()
    assert runtime.workspace.name == "Primary"
    assert runtime.state_root == state_root.resolve()
    assert not (workspace / ".corsia").exists()
    assert (workspace / "context").is_dir()
    assert (workspace / "skills").is_dir()
    assert (workspace / "shared" / "INDEX.md").is_file()
    assert (workspace / "projects").is_dir()
    assert (workspace / ".threads").is_dir()
    assert not (workspace / "working").exists()


def test_cli_init_does_not_write_bootstrap_when_workspace_scaffold_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    def fail_scaffold(_workspace):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "scaffold_workspace", fail_scaffold, raising=False)

    assert cli.cmd_init(_init_args(workspace, name="Primary")) == 2
    assert not bootstrap.exists()
    assert not state_root.exists()
    assert "scaffold" in capsys.readouterr().err.lower()


def test_cli_init_empty_prompt_accepts_the_default_workspace(
    tmp_path,
    monkeypatch,
    capsys,
):
    # Onboarding slice 1a (#3286) replaced the unexplained required prompt:
    # Enter accepts ~/corsia, and nothing lands in ~/Documents.
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    state_root = tmp_path / "state"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    prompts: list[str] = []

    def answer(prompt):
        prompts.append(prompt)
        return "  "

    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("builtins.input", answer)

    assert cli.cmd_init(_init_args(None)) == 0

    runtime = paths.load_bootstrap(bootstrap)
    assert runtime.workspace.root == (fake_home / "corsia").resolve()
    assert prompts == ["     Folder (press Enter for ~/corsia, or type another): "]
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "Where should your work live?",
        "     Your agents work freely inside this folder; anywhere else needs your approval.",
    ]
    warning = "     Documents, Desktop and Downloads aren't recommended (macOS TCC permission prompts)."
    assert (warning in lines) == (not sys.platform.startswith("linux"))
    assert not (fake_home / "Documents" / "corsia").exists()


def test_cli_init_reasks_after_protected_workspace_is_declined(tmp_path, monkeypatch):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    state_root = tmp_path / "state"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    prompts: list[str] = []
    answers = iter(["~/Documents/workspace", "n", "~/workspace"])

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("builtins.input", answer)

    assert cli.cmd_init(_init_args(None)) == 0

    runtime = paths.load_bootstrap(bootstrap)
    assert runtime.workspace.root == (fake_home / "workspace").resolve()
    assert prompts == [
        "     Folder (press Enter for ~/corsia, or type another): ",
        "     That folder is protected by macOS, so you'd see permission prompts. Use it anyway? [y/N] ",
        "     Folder (press Enter for ~/corsia, or type another): ",
    ]


def test_cli_init_accepts_a_protected_workspace_after_confirmation(tmp_path, monkeypatch):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    state_root = tmp_path / "state"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    answers = iter(["~/Library/Mobile Documents/workspace", "y"])

    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli.cmd_init(_init_args(None)) == 0

    runtime = paths.load_bootstrap(bootstrap)
    assert runtime.workspace.root == (fake_home / "Library" / "Mobile Documents" / "workspace").resolve()


def test_cli_refuses_to_overwrite_config_and_names_force(tmp_path, monkeypatch, capsys):
    import cli

    bootstrap = tmp_path / "config.json"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(tmp_path / "state"))
    assert cli.cmd_init(_init_args(tmp_path / "workspace", name="Primary")) == 0
    before = bootstrap.read_text()
    capsys.readouterr()

    assert cli.cmd_init(_init_args(tmp_path / "workspace")) == 1
    out = capsys.readouterr().out
    assert "init --force" in out and "setup choice" in out and str(bootstrap) in out
    assert bootstrap.read_text() == before


def test_cli_force_rejects_workspace_root_move(
    tmp_path,
    monkeypatch,
):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    first_root = tmp_path / "first-workspace"
    moved_root = tmp_path / "moved-workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    first_args = _init_args(first_root, name="Primary")
    moved_args = _init_args(moved_root, force=True)

    assert cli.cmd_init(first_args) == 0
    before = paths.load_bootstrap(bootstrap)
    assert cli.cmd_init(moved_args) == 2
    after = paths.load_bootstrap(bootstrap)

    assert after == before
    assert not moved_root.exists()


def test_cli_force_same_root_preserves_id_and_updates_name(
    tmp_path,
    monkeypatch,
):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    assert cli.cmd_init(_init_args(workspace, name="Primary")) == 0
    before = paths.load_bootstrap(bootstrap)
    assert cli.cmd_init(_init_args(workspace, force=True, name="Renamed")) == 0
    after = paths.load_bootstrap(bootstrap)

    assert after.workspace.workspace_id == before.workspace.workspace_id
    assert after.workspace.root == before.workspace.root
    assert after.workspace.name == "Renamed"
    assert after.state_root == before.state_root


def test_cli_force_rejects_blank_name_without_replacing_bootstrap(
    tmp_path,
    monkeypatch,
    capsys,
):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    assert cli.cmd_init(_init_args(workspace, name="Primary")) == 0
    before = bootstrap.read_bytes()

    result = cli.cmd_init(_init_args(workspace, force=True, name="   "))

    assert result == 2
    assert bootstrap.read_bytes() == before
    assert "workspace name" in capsys.readouterr().err.lower()


def test_cli_force_rejects_state_root_change(
    tmp_path,
    monkeypatch,
):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    first_state = tmp_path / "first-state"
    changed_state = tmp_path / "changed-state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(first_state))

    assert cli.cmd_init(_init_args(workspace, name="Primary")) == 0
    before = paths.load_bootstrap(bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(changed_state))

    result = cli.cmd_init(_init_args(workspace, force=True))

    assert result == 2
    assert paths.load_bootstrap(bootstrap) == before
    assert not changed_state.exists()


def test_cli_force_rejects_legacy_bootstrap_rewrite(
    tmp_path,
    monkeypatch,
):
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "legacy-workspace"
    legacy_state = workspace / ".corsia"
    legacy_db = legacy_state / "corsia.db"
    new_state = tmp_path / "platform-state"
    legacy_state.mkdir(parents=True)
    legacy_db.write_bytes(b"legacy-db")
    bootstrap.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )
    before = bootstrap.read_bytes()
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setattr(cli, "default_state_root", lambda: new_state)
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)

    result = cli.cmd_init(_init_args(workspace, force=True, name="Renamed"))

    assert result == 2
    assert bootstrap.read_bytes() == before
    assert legacy_db.read_bytes() == b"legacy-db"
    assert not new_state.exists()


def test_cli_init_rejects_workspace_nested_inside_state_root(
    tmp_path,
    monkeypatch,
    capsys,
):
    import cli

    bootstrap = tmp_path / "config.json"
    state_root = tmp_path / "state"
    workspace = state_root / "workspace"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    result = cli.cmd_init(_init_args(workspace, name="Primary"))

    assert result == 2
    assert not bootstrap.exists()
    assert not workspace.exists()
    assert "must not overlap" in capsys.readouterr().err.lower()


# --- Sandboxed bootstrap stat denial (EPERM, not ENOENT) -------------------
#
# Mirrors infra/config.py's own tri-state handling (see
# test_config_ci_sentinel.py): a hard-denied home directory makes even
# ``Path.exists()`` raise PermissionError instead of returning False.


class _EPERMPath(type(Path())):
    """A Path whose ``exists()`` raises PermissionError, simulating a sandbox
    denying even a stat of the real home directory."""

    def exists(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(self))


def test_cmd_init_fails_loud_when_bootstrap_stat_denied(tmp_path, monkeypatch, capsys):
    import cli

    monkeypatch.setattr(
        cli, "BOOTSTRAP_PATH", _EPERMPath(str(tmp_path / "config.json"))
    )

    result = cli.cmd_init(_init_args(tmp_path / "workspace"))

    assert result == 2
    assert "permission denied" in capsys.readouterr().err.lower()


def test_runtime_paths_for_workspace_fails_loud_when_bootstrap_stat_denied(
    tmp_path, monkeypatch
):
    import cli

    monkeypatch.setattr(
        cli, "BOOTSTRAP_PATH", _EPERMPath(str(tmp_path / "config.json"))
    )

    with pytest.raises(RuntimeError, match="permission denied"):
        cli._runtime_paths_for_workspace(tmp_path / "workspace")


def test_init_parser_accepts_workspace_name(tmp_path, monkeypatch):
    paths = _platform_paths()
    import cli

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    result = cli.main(
        [
            "init",
            "--path",
            str(workspace),
            "--name",
            "Named Workspace",
            "--setup",
            "local",
        ]
    )

    assert result == 0
    assert paths.load_bootstrap(bootstrap).workspace.name == "Named Workspace"


def test_cli_help_does_not_expose_retired_migrate_command(capsys):
    import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    assert "migrate" not in capsys.readouterr().out
