from __future__ import annotations

import importlib
import os
import re
import sqlite3
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest


APPLICATION_ID = "com.cebertowicz.corsia"


def _platform_paths():
    return importlib.import_module("infra.platform_paths")


def _service_file():
    return importlib.import_module("infra.service_file")


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
    paths = _service_file()
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
    # A state_dir that is not valid is an error, never a note: the launcher refuses
    # to start instead of opening a different, empty state root.
    assert paths.service_file_lines(service_file) == [
        "error=service.json has an invalid state_dir",
        "note=service.json: ignoring an invalid workspace value",
        "note=service.json: ignoring an invalid port value",
    ]
    service_file.write_text("[1]")
    assert paths.service_file_lines(service_file) == [
        "error=service.json unreadable: ValueError"
    ]


def test_state_dir_validity_follows_the_table_the_app_shell_also_reads():
    paths = _service_file()
    table = Path(__file__).parent / "fixtures" / "state_dir_validity.txt"
    cases = 0
    for line in table.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        expected, value = line.split("\t", 1)
        value = value.replace("\\u0007", "\u0007").replace("\\n", "\n")
        assert paths.state_dir_is_valid(value) is (expected == "valid"), line
        cases += 1
    assert cases >= 6
    assert paths.state_dir_is_valid(3) is False


def test_service_file_lines_accept_state_dir_without_an_unsupported_key_note(tmp_path):
    paths = _service_file()
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


def test_adoption_preflight_refuses_a_key_store_it_cannot_read_instead_of_calling_it_empty(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = tmp_path / "state"
    state.mkdir()
    connection = sqlite3.connect(state / "corsia.db")
    connection.execute("CREATE TABLE key_store_entries (name TEXT)")  # no value_ref column
    connection.commit()
    connection.close()

    with pytest.raises(snapshots.AdoptionRefused):
        snapshots.stored_key_names(state)


def test_adoption_preflight_passes_when_every_expected_key_is_stored(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(
        tmp_path, {"provider-openai": "stored-openai-value", "provider-anthropic": "stored-anthropic-value"}
    )
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (0, 1 << 60))

    snapshots.check_adoption_preflight(state, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
    assert snapshots.preflight_main([str(state), "OPENAI_API_KEY"]) == 0


def test_adoption_preflight_refuses_data_from_a_newer_schema_than_the_backend_opens(tmp_path, monkeypatch, capsys):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    schema = importlib.import_module("infra.schema")
    state = _adoptable_state(tmp_path, {})
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (0, 1 << 60))
    database = state / "corsia.db"

    for version, expected in ((schema.SCHEMA_VERSION, 0), (schema.SCHEMA_VERSION - 1, 0), (schema.SCHEMA_VERSION + 1, snapshots.DATA_NEWER)):
        with sqlite3.connect(database) as connection:
            connection.execute(f"PRAGMA user_version={version}")
        assert snapshots.database_schema_version(database) == version
        assert snapshots.preflight_main([str(state)]) == expected
    assert snapshots.DATA_NEWER == 5
    assert capsys.readouterr().err == ""  # nothing to forward: the app words this itself
    # A file whose header cannot be read is not called newer.
    assert snapshots.database_schema_version(state / "missing.db") is None


def _restart_request(state: Path, status: str) -> None:
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute(
            "create table if not exists self_restart_requests "
            "(id text primary key, status text not null default 'in_flight', requested_at text not null)"
        )
        connection.execute("insert into self_restart_requests (id, status, requested_at) values ('r1', ?, 'now')", (status,))


def test_adoption_preflight_refuses_while_the_source_is_restarting_itself(tmp_path, monkeypatch, capsys):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (0, 1 << 60))
    _restart_request(state, "in_flight")

    assert snapshots.preflight_main([str(state)]) == snapshots.RESTARTING == 4
    assert capsys.readouterr().err.strip() == (
        "Your existing CORSIA is restarting itself. Wait a minute, then choose Try again."
    )
    assert snapshots.restart_in_flight(state) is True
    # The look is read-only: the request is still there for the source to settle.
    with sqlite3.connect(state / "corsia.db") as connection:
        assert connection.execute("select status from self_restart_requests").fetchall() == [("in_flight",)]


def _ago(seconds: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _requests_table(state: Path) -> None:
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute(
            "create table self_restart_requests "
            "(id text primary key, status text not null default 'in_flight', requested_at text not null, phase text)"
        )


def _add_request(state: Path, name: str, status: str, requested_at: str, phase: str | None = None) -> None:
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute("insert into self_restart_requests values (?, ?, ?, ?)", (name, status, requested_at, phase))


def test_the_age_of_an_in_flight_restart_comes_from_its_youngest_active_row(tmp_path):
    from datetime import UTC, datetime, timedelta

    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    assert snapshots.restart_in_flight_age(state) is None  # no table at all
    _requests_table(state)
    _add_request(state, "settled", "completed", _ago(5), "restarting")
    assert snapshots.restart_in_flight_age(state) is None
    _add_request(state, "old", "in_flight", _ago(2400), "draining")
    assert 2399 < snapshots.restart_in_flight_age(state) < 2410, "an operational hold never expires"
    _add_request(state, "young", "in_flight", _ago(30), "holding")
    # A time without a zone is read as UTC, as the backend does.
    naive = (datetime.now(UTC) - timedelta(seconds=90)).replace(tzinfo=None).isoformat()
    _add_request(state, "naive", "in_flight", naive, "holding")
    assert 29 < snapshots.restart_in_flight_age(state) < 40, "the youngest row decides"
    _add_request(state, "odd", "in_flight", "now", "holding")
    assert snapshots.restart_in_flight_age(state) == 0.0, "an unreadable time is never called stuck"
    assert snapshots.restart_in_flight(state) is True


def test_a_request_the_backend_does_not_count_as_in_flight_is_not_one_here_either(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    _requests_table(state)
    # Legacy phaseless row past its 600 s guard, and a row in a phase that is not a hold.
    _add_request(state, "legacy", "in_flight", _ago(601))
    _add_request(state, "idle", "in_flight", _ago(5), "complete")
    assert snapshots.restart_in_flight_age(state) is None
    # A phaseless row inside the guard still counts.
    _add_request(state, "fresh", "in_flight", _ago(20))
    assert 19 < snapshots.restart_in_flight_age(state) < 30


def test_a_legacy_table_without_a_phase_column_follows_the_same_rule(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute(
            "create table self_restart_requests "
            "(id text primary key, status text not null default 'in_flight', requested_at text not null)"
        )
        connection.execute("insert into self_restart_requests values ('old', 'in_flight', ?)", (_ago(2400),))
    assert snapshots.restart_in_flight_age(state) is None
    with sqlite3.connect(state / "corsia.db") as connection:
        connection.execute("insert into self_restart_requests values ('new', 'in_flight', ?)", (_ago(40),))
    assert 39 < snapshots.restart_in_flight_age(state) < 50


def test_the_app_and_the_backend_agree_on_what_is_in_flight(tmp_path):
    """The one rule, over every phase and a range of ages, against the backend's own reader."""
    from automation import self_restart

    snapshots = importlib.import_module("infra.adoption_snapshot")
    phases = [None, "", "holding", "steering", "draining", "ready", "restarting", "resuming", "complete", "weird"]
    for number, (phase, age) in enumerate((p, a) for p in phases for a in (5, 599, 601, 5000)):
        state = tmp_path / f"state{number}"
        state.mkdir()
        _requests_table(state)
        _add_request(state, "r", "in_flight", _ago(age), phase)
        connection = sqlite3.connect(state / "corsia.db")
        connection.row_factory = sqlite3.Row
        try:
            backend = self_restart.in_flight_request(db=connection) is not None
        finally:
            connection.close()
        assert (snapshots.restart_in_flight_age(state) is not None) == backend, (phase, age)


def test_the_restart_rule_imports_nothing_that_resolves_a_workspace():
    """The app's preflight runs in a bare interpreter: no infra module may load with the rule."""
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import automation.restart_rule; "
        "print([m for m in sys.modules if m == 'infra' or m.startswith('infra.')])"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(backend)],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_adoption_preflight_ignores_a_settled_restart_and_a_missing_table(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (0, 1 << 60))

    snapshots.check_adoption_preflight(state, [])  # no self_restart_requests table at all
    _restart_request(state, "completed")
    snapshots.check_adoption_preflight(state, [])


def test_adoption_preflight_refuses_when_the_backup_and_reserve_do_not_fit(tmp_path, monkeypatch, capsys):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (1 << 60, 1 << 30))

    assert snapshots.preflight_main([str(state)]) == snapshots.REFUSED
    message = capsys.readouterr().err.strip()
    # The measured numbers and the next step, in the person's terms.
    assert re.fullmatch(
        r"The backup needs [\d.]+ GB free and this disk has [\d.]+ GB\. Free up space, then choose Try again\.",
        message,
    )
    assert not (state / "backups").exists()


def test_adoption_preflight_refuses_when_only_prunable_backups_make_room(tmp_path, monkeypatch, capsys):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    # Room is claimed, but only by superseded backups that are pruned after the snapshot is written.
    monkeypatch.setattr(snapshots, "backup_room", lambda database: (0, 1 << 60))
    monkeypatch.setattr(snapshots.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))

    assert snapshots.preflight_main([str(state)]) == snapshots.REFUSED
    # The figure printed is the one that failed: what is free now, not the larger prunable total.
    message = capsys.readouterr().err.strip()
    assert re.fullmatch(
        r"The backup needs [\d.]+ GB and this disk has 0\.0 GB free now\. Free up space, then choose Try again\.",
        message,
    )


def test_adoption_preflight_asks_the_migration_backup_room_for_its_own_database(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    asked = []

    def room(database):
        asked.append(database)
        return 10, 1 << 40

    monkeypatch.setattr(snapshots, "backup_room", room)

    snapshots.check_adoption_preflight(state, [])

    assert asked == [state.resolve() / "corsia.db"]


def test_adoption_snapshot_keeps_links_as_links_through_a_restore(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "auth.json").write_text("credential")
    (state / "linked-file").symlink_to(outside / "auth.json")
    (state / "linked-dir").symlink_to(outside, target_is_directory=True)
    (state / "dangling").symlink_to(tmp_path / "gone")
    (state / "keys" / "inner-link").symlink_to(outside / "auth.json")

    snapshot = snapshots.create_adoption_snapshot(state)

    # Nothing a link points at is copied into the backup.
    assert not any(path.name == "auth.json" for path in snapshot.rglob("*") if not path.is_symlink())
    (state / "linked-dir").unlink()
    (state / "linked-dir").mkdir()
    snapshots.restore_adoption_snapshot(state, snapshot)

    for name, target in {
        "linked-file": outside / "auth.json",
        "linked-dir": outside,
        "dangling": tmp_path / "gone",
    }.items():
        assert (state / name).is_symlink() and (state / name).readlink() == target
    assert (state / "keys" / "inner-link").is_symlink()
    assert (outside / "auth.json").read_text() == "credential"


def test_the_preflight_size_walk_skips_files_the_running_service_removes(tmp_path, monkeypatch):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    (state / "keys" / "kept").write_bytes(b"x" * 10)
    real_walk = os.walk

    def walk_with_a_vanished_file(top, *args, **kwargs):
        for directory, names, files in real_walk(top, *args, **kwargs):
            yield directory, names, [*files, "removed-after-listing"]

    monkeypatch.setattr(snapshots.os, "walk", walk_with_a_vanished_file)

    assert snapshots._snapshot_size(state.resolve()) >= 10
    assert snapshots._entry_size(state / "gone") == 0


def test_a_new_attempt_keeps_only_the_newest_earlier_adoption_snapshot(tmp_path):
    snapshots = importlib.import_module("infra.adoption_snapshot")
    state = _adoptable_state(tmp_path, {})
    backups = state / "backups"
    backups.mkdir()
    for age, name in enumerate(("adopt-oldest", "adopt-older", "adopt-newest-earlier")):
        (backups / name).mkdir()
        (backups / name / "corsia.db").write_text(name)
        os.utime(backups / name, (1_000_000 + age, 1_000_000 + age))
    (backups / "corsia.db.pre-migration").write_text("not an adoption snapshot")

    snapshot = snapshots.create_adoption_snapshot(state)

    assert sorted(path.name for path in backups.iterdir()) == sorted(
        ["adopt-newest-earlier", "corsia.db.pre-migration", snapshot.name]
    )
    # A finished adoption's own snapshot stays: it is the safety net.
    assert snapshot.is_dir()


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
