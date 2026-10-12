"""Regression coverage for the operational SQLite backup path resolver."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from infra.platform_paths import default_state_root


def _write_marker_database(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        database.execute("INSERT INTO marker(value) VALUES (?)", (marker,))


def _run_backup(home: Path, **environment_overrides: str) -> str:
    script = Path(__file__).resolve().parents[2] / "scripts" / "corsia-backup-db"
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("CORSIA_WORKSPACE", None)
    environment.pop("CORSIA_STATE_DIR", None)
    environment.update(environment_overrides)
    result = subprocess.run(
        [str(script)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    backups = list((home / "backups" / "corsia").glob("corsia-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as database:
        return database.execute("SELECT value FROM marker").fetchone()[0]


def _write_v2_bootstrap(home: Path, workspace: Path, state_root: Path) -> None:
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-test",
                    "name": "Test",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        ),
        encoding="utf-8",
    )


def test_backup_uses_v2_bootstrap_state_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()
    _write_marker_database(state_root / "corsia.db", "canonical-state")
    _write_marker_database(
        workspace / ".corsia" / "corsia.db",
        "stale-workspace-state",
    )
    stale_override = tmp_path / "stale-override"
    _write_marker_database(
        stale_override / ".corsia" / "corsia.db",
        "stale-environment-state",
    )
    _write_v2_bootstrap(home, workspace, state_root)

    assert _run_backup(home, CORSIA_WORKSPACE=str(stale_override)) == "canonical-state"


def test_backup_retention_keeps_only_the_requested_count(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()
    _write_marker_database(state_root / "corsia.db", "production-state")
    _write_v2_bootstrap(home, workspace, state_root)
    backup_dir = home / "backups" / "corsia"
    _write_marker_database(backup_dir / "corsia-20000101-000000.db", "old-state")
    _write_marker_database(backup_dir / "corsia-20000102-000000.db", "older-state")
    script = Path(__file__).resolve().parents[2] / "scripts" / "corsia-backup-db"
    environment_vars = os.environ.copy()
    environment_vars.pop("CORSIA_WORKSPACE", None)
    environment_vars.pop("CORSIA_STATE_DIR", None)

    result = subprocess.run(
        [str(script), "--keep", "1"],
        env={**environment_vars, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    backups = list(backup_dir.glob("corsia-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as database:
        assert database.execute("SELECT value FROM marker").fetchone()[0] == (
            "production-state"
        )


def test_backup_retention_never_prunes_unrelated_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()
    _write_marker_database(state_root / "corsia.db", "production-state")
    _write_v2_bootstrap(home, workspace, state_root)
    backup_dir = home / "backups" / "corsia"
    unrelated = backup_dir / "corsia-prod-20000101-000000.db"
    _write_marker_database(unrelated, "pre-collapse-backup")
    script = Path(__file__).resolve().parents[2] / "scripts" / "corsia-backup-db"
    environment_vars = os.environ.copy()
    environment_vars.pop("CORSIA_WORKSPACE", None)
    environment_vars.pop("CORSIA_STATE_DIR", None)

    result = subprocess.run(
        [str(script), "--keep", "1"],
        env={**environment_vars, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert unrelated.is_file()


def test_backup_preserves_legacy_bootstrap_support(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "legacy-workspace"
    _write_marker_database(
        workspace / ".corsia" / "corsia.db",
        "legacy-state",
    )
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )

    assert _run_backup(home) == "legacy-state"


def test_backup_uses_platform_state_for_env_only_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = default_state_root(env={}, platform=sys.platform, home=home)
    _write_marker_database(state_root / "corsia.db", "platform-state")

    assert _run_backup(home, CORSIA_WORKSPACE=str(workspace)) == "platform-state"


def _backup_with_keys(home: Path, state_root: Path, *extra: str) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve().parents[2] / "scripts" / "corsia-backup-db"
    environment_vars = os.environ.copy()
    environment_vars.pop("CORSIA_WORKSPACE", None)
    environment_vars.pop("CORSIA_STATE_DIR", None)
    result = subprocess.run(
        [str(script), *extra],
        env={**environment_vars, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_backup_copies_the_key_store_file_privately_and_rotates_it_with_the_database(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()
    _write_marker_database(state_root / "corsia.db", "production-state")
    (state_root / "keys").mkdir()
    (state_root / "keys" / "values.json").write_text('{"web-search": "synthetic-value"}', encoding="utf-8")
    _write_v2_bootstrap(home, workspace, state_root)
    backup_dir = home / "backups" / "corsia"
    old = backup_dir / "corsia-20000101-000000.db"
    _write_marker_database(old, "old-state")
    old_keys = backup_dir / "corsia-20000101-000000.keys.json"
    old_keys.write_text("{}", encoding="utf-8")

    result = _backup_with_keys(home, state_root, "--keep", "1")

    copies = list(backup_dir.glob("corsia-*.keys.json"))
    assert len(copies) == 1 and copies[0] != old_keys and not old_keys.exists() and not old.exists()
    assert copies[0].read_text(encoding="utf-8") == '{"web-search": "synthetic-value"}'
    assert copies[0].with_suffix("").with_suffix(".db").exists()
    assert oct(copies[0].stat().st_mode & 0o777) == "0o600"
    assert oct(backup_dir.stat().st_mode & 0o777) == "0o700"
    assert "synthetic-value" not in result.stdout + result.stderr


def test_backup_without_a_key_store_file_writes_no_key_copy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()
    _write_marker_database(state_root / "corsia.db", "production-state")
    _write_v2_bootstrap(home, workspace, state_root)

    _backup_with_keys(home, state_root)

    assert list((home / "backups" / "corsia").glob("*.keys.json")) == []
