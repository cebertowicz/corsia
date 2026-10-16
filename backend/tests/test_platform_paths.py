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


def _fake_app_module(tmp_path) -> Path:
    module = (
        tmp_path / "Applications" / "CORSIA.app" / "Contents" / "Resources"
        / "backend" / "infra" / "platform_paths.py"
    )
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    return module


def test_app_bundle_root_is_a_path_check_on_the_module_location(tmp_path, monkeypatch):
    paths = _platform_paths()
    module = _fake_app_module(tmp_path)

    assert paths.app_bundle_root(module) == tmp_path / "Applications" / "CORSIA.app"
    assert paths.app_bundle_root(tmp_path / "checkout" / "backend" / "infra" / "x.py") is None
    # The checkout this test runs from is never an app bundle, whatever the env says.
    monkeypatch.setenv("CORSIA_DESKTOP_BUNDLE", "1")
    assert paths.app_bundle_root() is None


def test_default_state_root_is_the_state_child_of_the_app_folder_in_the_app(tmp_path):
    paths = _platform_paths()

    result = paths.default_state_root(
        env={},
        platform="darwin",
        home=tmp_path,
        app_bundle=tmp_path / "Applications" / "CORSIA.app",
    )

    assert result == tmp_path / "Library" / "Application Support" / "CORSIA" / "state"
    assert result.parent == paths.app_metadata_dir(tmp_path)


def test_default_state_root_for_a_source_install_never_uses_the_app_folder(tmp_path):
    paths = _platform_paths()
    app_folder = tmp_path / "Library" / "Application Support" / "CORSIA"
    app_folder.mkdir(parents=True)

    result = paths.default_state_root(
        env={},
        platform="darwin",
        home=tmp_path,
        app_bundle=None,
    )

    assert result == (
        tmp_path / "Library" / "Application Support" / APPLICATION_ID / "state"
    )
    assert not result.is_relative_to(app_folder)


def test_default_state_root_reads_app_mode_from_the_module_location(tmp_path):
    paths = _platform_paths()

    # This checkout's module is not inside a bundle: the source-install rule.
    assert paths.default_state_root(env={}, platform="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / APPLICATION_ID / "state"
    )


def _preview_folder(tmp_path) -> Path:
    app_dir = tmp_path / "CORSIA"
    (app_dir / "logs").mkdir(parents=True)
    (app_dir / "uploads").mkdir()
    (app_dir / "corsia.db").write_text("db", encoding="utf-8")
    (app_dir / "corsia.db-wal").write_text("wal", encoding="utf-8")
    (app_dir / "logs" / "service.log").write_text("log", encoding="utf-8")
    (app_dir / "service.json").write_text("{}", encoding="utf-8")
    return app_dir


def test_preview_layout_moves_operational_state_into_state_once(tmp_path):
    paths = _platform_paths()
    app_dir = _preview_folder(tmp_path)

    moved = paths.move_preview_layout(app_dir, app_dir / "state")

    assert moved == ["corsia.db", "corsia.db-wal", "uploads", "logs"]
    assert (app_dir / "state" / "corsia.db").read_text(encoding="utf-8") == "db"
    assert (app_dir / "state" / "logs" / "service.log").is_file()
    assert (app_dir / "service.json").is_file(), "app metadata stays beside state/"
    assert not (app_dir / "corsia.db").exists()
    assert sorted(path.name for path in app_dir.iterdir()) == ["service.json", "state"]
    assert paths.move_preview_layout(app_dir, app_dir / "state") == []


def test_preview_layout_move_never_touches_an_existing_state_root(tmp_path):
    paths = _platform_paths()
    app_dir = _preview_folder(tmp_path)
    (app_dir / "state").mkdir()

    assert paths.move_preview_layout(app_dir, app_dir / "state") == []
    assert (app_dir / "corsia.db").is_file()
    assert list((app_dir / "state").iterdir()) == []


def test_preview_layout_move_ignores_a_folder_without_a_database(tmp_path):
    paths = _platform_paths()
    app_dir = tmp_path / "CORSIA"
    app_dir.mkdir()
    (app_dir / "service.json").write_text("{}", encoding="utf-8")

    assert paths.move_preview_layout(app_dir, app_dir / "state") == []
    assert not (app_dir / "state").exists()


def test_preview_layout_move_ignores_a_state_root_outside_the_app_folder(tmp_path):
    paths = _platform_paths()
    app_dir = _preview_folder(tmp_path)

    assert paths.move_preview_layout(app_dir, tmp_path / "elsewhere" / "state") == []
    assert (app_dir / "corsia.db").is_file()


def test_an_interrupted_preview_layout_move_is_finished_next_time(tmp_path):
    paths = _platform_paths()
    app_dir = _preview_folder(tmp_path)
    staging = app_dir / ".state.moving"
    staging.mkdir()
    os.rename(app_dir / "corsia.db", staging / "corsia.db")

    moved = paths.move_preview_layout(app_dir, app_dir / "state")

    assert moved == ["corsia.db-wal", "uploads", "logs"]
    assert (app_dir / "state" / "corsia.db").read_text(encoding="utf-8") == "db"
    assert not staging.exists()


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
