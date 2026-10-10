from __future__ import annotations

import importlib
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


APPLICATION_ID = "com.cebertowicz.corsia"


def _platform_paths():
    return importlib.import_module("infra.platform_paths")


def test_default_state_root_uses_macos_application_support(tmp_path):
    paths = _platform_paths()

    result = paths.default_state_root(
        env={},
        platform="darwin",
        home=tmp_path,
    )

    assert result == (
        tmp_path / "Library" / "Application Support" / APPLICATION_ID / "state"
    )


def test_default_state_root_does_not_claim_existing_application_metadata(tmp_path):
    paths = _platform_paths()
    application_root = tmp_path / "Library" / "Application Support" / APPLICATION_ID
    application_root.mkdir(parents=True)
    config = application_root / "config.json"
    config.write_text('{"server_url":"http://localhost"}\n', encoding="utf-8")

    result = paths.default_state_root(
        env={},
        platform="darwin",
        home=tmp_path,
    )

    assert result == application_root / "state"
    assert config.read_text(encoding="utf-8") == ('{"server_url":"http://localhost"}\n')


def test_default_state_root_uses_xdg_state_home(tmp_path):
    paths = _platform_paths()
    xdg_state = tmp_path / "xdg-state"

    result = paths.default_state_root(
        env={"XDG_STATE_HOME": str(xdg_state)},
        platform="linux",
        home=tmp_path / "home",
    )

    assert result == xdg_state / APPLICATION_ID / "state"


def test_default_state_root_uses_xdg_fallback(tmp_path):
    paths = _platform_paths()

    result = paths.default_state_root(
        env={},
        platform="linux",
        home=tmp_path,
    )

    assert result == tmp_path / ".local" / "state" / APPLICATION_ID / "state"


def test_default_state_root_uses_windows_local_app_data(tmp_path):
    paths = _platform_paths()
    local_app_data = tmp_path / "LocalAppData"

    result = paths.default_state_root(
        env={"LOCALAPPDATA": str(local_app_data)},
        platform="win32",
        home=tmp_path / "home",
    )

    assert result == local_app_data / APPLICATION_ID / "state"


def test_state_directory_override_wins_on_every_platform(tmp_path):
    paths = _platform_paths()
    override = tmp_path / "corsia-state"

    result = paths.default_state_root(
        env={
            "CORSIA_STATE_DIR": str(override),
            "XDG_STATE_HOME": str(tmp_path / "ignored"),
        },
        platform="linux",
        home=tmp_path / "home",
    )

    assert result == override


def test_workspace_selection_and_runtime_paths_are_immutable(tmp_path):
    paths = _platform_paths()
    selection = paths.WorkspaceSelection(
        workspace_id="workspace-123",
        root=tmp_path / "workspace",
        name="Primary",
    )
    runtime = paths.RuntimePaths(
        workspace=selection,
        state_root=tmp_path / "state",
    )

    with pytest.raises(FrozenInstanceError):
        selection.root = Path("/other")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        runtime.state_root = Path("/other")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("workspace_id", "name", "missing_field"),
    [
        ("", "Primary", "workspace id"),
        ("   ", "Primary", "workspace id"),
        ("workspace-123", "", "workspace name"),
        ("workspace-123", "   ", "workspace name"),
    ],
)
def test_workspace_selection_rejects_blank_identity_fields(
    tmp_path,
    workspace_id,
    name,
    missing_field,
):
    paths = _platform_paths()

    with pytest.raises(ValueError, match=missing_field):
        paths.WorkspaceSelection(
            workspace_id=workspace_id,
            root=tmp_path / "workspace",
            name=name,
        )


def test_runtime_paths_canonicalize_relative_roots(tmp_path, monkeypatch):
    paths = _platform_paths()
    monkeypatch.chdir(tmp_path)

    runtime = paths.RuntimePaths(
        workspace=paths.WorkspaceSelection(
            workspace_id="workspace-123",
            root=Path("workspace"),
            name="Primary",
        ),
        state_root=Path("state"),
    )

    assert runtime.workspace.root == (tmp_path / "workspace").resolve()
    assert runtime.state_root == (tmp_path / "state").resolve()


def test_atomic_write_fsyncs_file_then_replacement_then_directory(
    tmp_path,
    monkeypatch,
):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    runtime = paths.RuntimePaths(
        workspace=paths.WorkspaceSelection(
            workspace_id="workspace-123",
            root=tmp_path / "workspace",
            name="Primary",
        ),
        state_root=tmp_path / "state",
    )
    events: list[str] = []
    real_fsync = paths.os.fsync
    real_replace = paths.os.replace

    def record_fsync(descriptor):
        mode = os.fstat(descriptor).st_mode
        events.append("directory-fsync" if stat.S_ISDIR(mode) else "file-fsync")
        real_fsync(descriptor)

    def record_replace(source, destination):
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(paths.os, "fsync", record_fsync)
    monkeypatch.setattr(paths.os, "replace", record_replace)
    paths.write_bootstrap(bootstrap, runtime)

    assert events == ["file-fsync", "replace", "directory-fsync"]


def test_directory_fsync_is_noop_on_windows(tmp_path, monkeypatch):
    paths = _platform_paths()
    monkeypatch.setattr(paths.sys, "platform", "win32")

    def fail_open(*_args, **_kwargs):
        raise AssertionError("Windows directory fsync must not call os.open")

    monkeypatch.setattr(paths.os, "open", fail_open)

    paths._fsync_directory(tmp_path)


@pytest.mark.parametrize(
    "state_relative",
    [Path("."), Path("runtime-state")],
)
def test_runtime_paths_reject_state_root_inside_workspace(
    tmp_path,
    state_relative,
):
    paths = _platform_paths()
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="state root must be outside workspace"):
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=workspace,
                name="Primary",
            ),
            state_root=workspace / state_relative,
        )


def test_runtime_paths_reject_workspace_inside_state_root(tmp_path):
    paths = _platform_paths()
    state_root = tmp_path / "state"

    with pytest.raises(ValueError, match="must not overlap"):
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=state_root / "workspace",
                name="Primary",
            ),
            state_root=state_root,
        )
