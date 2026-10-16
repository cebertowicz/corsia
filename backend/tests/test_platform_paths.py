from __future__ import annotations

import importlib
import os
import re
import sqlite3
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


def test_app_folder_name_is_one_value_across_the_app_launcher_and_backend():
    paths = _platform_paths()
    repo = Path(__file__).resolve().parents[2]
    rust = (repo / "desktop" / "src-tauri" / "src" / "app_paths.rs").read_text()
    launcher = (repo / "desktop" / "scripts" / "corsia-service").read_text()
    app_mode = (repo / "scripts" / "corsia.d" / "app_mode.sh").read_text()

    rust_folder = re.findall(r'^pub const APP_FOLDER: &str = "([^"]*)";$', rust, re.M)
    launcher_folder = re.findall(r"^CORSIA_APP_FOLDER=(\S+)$", launcher, re.M)

    assert rust_folder == launcher_folder == [paths.APP_FOLDER] == ["CORSIA"]
    # The bundled lifecycle script asks the backend for every app path rather
    # than spelling the folder a fourth time.
    assert "Application Support" not in app_mode
    # It finds the app binary's staged and previous copies by the same names.
    stage = (repo / "desktop" / "src-tauri" / "src" / "update_stage.rs").read_text()
    rust_names = re.findall(r'with_file_name\("(CORSIA\.app\.\w+)"\)', stage)
    shell_names = re.findall(r'/(CORSIA\.app\.\w+)"', app_mode)
    assert sorted(rust_names) == sorted(shell_names) == [
        "CORSIA.app.previous",
        "CORSIA.app.staged",
    ]


def test_service_file_lines_honour_only_the_state_workspace_and_port(tmp_path):
    paths = _platform_paths()
    service_file = tmp_path / "service.json"
    service_file.write_text(
        '{"state_dir": "/state", "workspace": "/w", "port": 4100, "PATH": "/evil", "x\\u0007": 1}'
    )

    assert paths.service_file_lines(service_file) == [
        "note=service.json: ignoring unsupported key PATH",
        "note=service.json: ignoring unsupported key <unprintable name>",
        "state_dir=/state",
        "workspace=/w",
        "port=4100",
    ]
    service_file.write_text('{"state_dir": "relative", "workspace": "relative", "port": true}')
    assert paths.service_file_lines(service_file) == [
        "note=service.json: ignoring an invalid state_dir value",
        "note=service.json: ignoring an invalid workspace value",
        "note=service.json: ignoring an invalid port value",
    ]
    service_file.write_text("[1]")
    assert paths.service_file_lines(service_file) == [
        "note=service.json unreadable: ValueError"
    ]


def test_service_file_lines_accept_state_dir_without_an_unsupported_key_note(tmp_path):
    paths = _platform_paths()
    service_file = tmp_path / "service.json"
    service_file.write_text('{"state_dir": "/state"}')

    assert paths.service_file_lines(service_file) == ["state_dir=/state"]


def test_adoption_snapshot_restores_database_files_and_permissions(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = tmp_path / "Application Support" / "com.cebertowicz.corsia" / "state"
    state.mkdir(parents=True)
    database = state / "corsia.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table agents (id text)")
        connection.execute("insert into agents values ('before-adopt')")
    database.chmod(0o600)
    keys = state / "keys"
    keys.mkdir(mode=0o700)
    key = keys / "provider"
    key.write_text("kept-private")
    key.chmod(0o600)
    (state / "devices").mkdir(mode=0o700)
    (state / "devices" / "registered").write_text("device")

    snapshot = snapshots.create_adoption_snapshot(state)
    with sqlite3.connect(database) as connection:
        connection.execute("insert into agents values ('new-build')")
    (state / "keys" / "new-key").write_text("new")
    (state / "new-build-file").write_text("new")
    (state / "corsia.db-wal").write_text("wal")
    (state / "corsia.db-shm").write_text("shm")

    snapshots.restore_adoption_snapshot(state, snapshot)

    with sqlite3.connect(database) as connection:
        assert connection.execute("select id from agents order by id").fetchall() == [("before-adopt",)]
    assert not (state / "new-build-file").exists()
    assert not (state / "keys" / "new-key").exists()
    assert not (state / "corsia.db-wal").exists()
    assert not (state / "corsia.db-shm").exists()
    assert key.read_text() == "kept-private"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def _adoptable_state(tmp_path, stored: dict[str, str]) -> Path:
    """A state root whose one values file holds ``stored`` (entry name -> value), as the key store writes it."""
    import json

    state = tmp_path / "state"
    (state / "keys").mkdir(parents=True)
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute(
            "create table key_store_entries (name text primary key, created_at text not null, value_ref text)"
        )
        values = {}
        for name, value in stored.items():
            reference = f"{name}.0123456789abcdef"
            values[reference] = value
            connection.execute(
                "insert into key_store_entries values (?, 'now', ?)", (name, reference)
            )
        # A row with no value, and a leftover from the key store's temporary file.
        connection.execute("insert into key_store_entries values ('provider-groq', 'now', null)")
    (state / "keys" / "values.json").write_text(json.dumps(values))
    (state / "keys" / ".values.json.deadbeef.tmp").write_text("{}")
    return state


def test_adoption_preflight_names_the_stored_keys_it_misses_in_the_single_values_file(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {"provider-openai": "stored-openai-value"})

    with pytest.raises(snapshots.AdoptionRefused) as refused:
        snapshots.check_adoption_preflight(state, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"])

    message = str(refused.value)
    assert message == (
        "These provider keys aren’t stored in CORSIA yet: ANTHROPIC_API_KEY. "
        "Add them in CORSIA › Settings › Keys, then choose Try again."
    )
    assert "ANTHROPIC_API_KEY" in message
    assert "OPENAI_API_KEY" not in message
    assert "stored-openai-value" not in message


def test_adoption_preflight_passes_when_every_expected_key_is_stored(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(
        tmp_path, {"provider-openai": "stored-openai-value", "provider-anthropic": "stored-anthropic-value"}
    )
    monkeypatch.setattr(snapshots, "_FREE_FLOOR_BYTES", 0)

    snapshots.check_adoption_preflight(state, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
    assert snapshots.preflight_main([str(state), "OPENAI_API_KEY"]) == 0


def test_adoption_preflight_refuses_when_the_backup_and_reserve_do_not_fit(tmp_path, monkeypatch, capsys):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    monkeypatch.setattr(snapshots, "_FREE_FLOOR_BYTES", 1 << 60)

    assert snapshots.preflight_main([str(state)]) == snapshots.REFUSED
    message = capsys.readouterr().err.strip()
    # The measured numbers and the next step, in the person's terms.
    assert re.fullmatch(
        r"The backup needs [\d.]+ GB free and this disk has [\d.]+ GB\. Free up space, then choose Try again\.",
        message,
    )
    assert not (state / "backups").exists()


def test_a_failed_snapshot_copy_leaves_no_partial_backup_behind(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})

    def full_disk(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snapshots, "_copy_entry", full_disk)

    with pytest.raises(OSError):
        snapshots.create_adoption_snapshot(state)

    assert list((state / "backups").iterdir()) == []


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

    assert moved == ["corsia.db", "corsia.db-wal", "logs", "uploads"]
    assert (app_dir / "state" / "corsia.db").read_text(encoding="utf-8") == "db"
    assert (app_dir / "state" / "logs" / "service.log").is_file()
    assert (app_dir / "service.json").is_file(), "app metadata stays beside state/"
    assert not (app_dir / "corsia.db").exists()
    assert sorted(path.name for path in app_dir.iterdir()) == ["service.json", "state"]
    assert paths.move_preview_layout(app_dir, app_dir / "state") == []


def test_preview_layout_moves_every_state_entry_but_app_metadata(tmp_path):
    paths = _platform_paths()
    app_dir = tmp_path / "CORSIA"
    app_dir.mkdir()
    state_entries = {
        "corsia.db",
        "documents",
        "assets",
        "locks",
        "security",
        "worktree_reaper",
        "reviewer",
        "local_setup_installed_omlx",
        "local_setup_existing_omlx_consented",
        "local-setup-operation.json",
    }
    for name in state_entries:
        path = app_dir / name
        if "." in name:
            path.write_text(name, encoding="utf-8")
        else:
            path.mkdir()
            (path / "state").write_text(name, encoding="utf-8")
    (app_dir / "service.json").write_text("{}", encoding="utf-8")

    moved = paths.move_preview_layout(app_dir, app_dir / "state")

    assert set(moved) == state_entries
    assert {path.name for path in (app_dir / "state").iterdir()} == state_entries
    assert sorted(path.name for path in app_dir.iterdir()) == ["service.json", "state"]


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

    assert moved == ["corsia.db-wal", "logs", "uploads"]
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
