"""Dormant migration script scope grouping and dry-run safety."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


def _script_module():
    script = Path(__file__).parents[2] / "scripts/migrations/migrate_working_to_threads.py"
    spec = importlib.util.spec_from_file_location("thread_migration_script", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT);
        CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT, bootloader_files TEXT);
        """
    )
    return db


def test_dormant_script_groups_ids_by_their_actual_scope(tmp_path, monkeypatch):
    from tests.project_ref_helpers import project_ref, select_workspace

    module = _script_module()
    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "alpha"
    project.mkdir(parents=True)
    db = _db()
    db.execute("INSERT INTO projects (id, name, path) VALUES (?, ?, ?)", ("p1", "Alpha", project_ref("alpha")))
    db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("missing", "Missing"))
    db.executemany(
        "INSERT INTO threads (id, project_id, bootloader_files) VALUES (?, ?, ?)",
        [
            ("workspace-thread", None, '["/scope/working/workspace-thread/context.md"]'),
            ("project-thread", "p1", '["/scope/working/project-thread/context.md"]'),
            ("unresolved-thread", "missing", "[]"),
        ],
    )

    groups = module._scope_groups(db, workspace)

    by_root = {root: (ids, durable) for root, ids, durable in groups}
    assert by_root[workspace.resolve()][0] == {"workspace-thread"}
    assert by_root[project.resolve()][0] == {"project-thread"}
    assert by_root[project.resolve()][1] == {"project-thread": {"context.md"}}
    assert all("unresolved-thread" not in ids for ids, _durable in by_root.values())
    db.close()


def test_dormant_script_rejects_apply_before_opening_the_database(monkeypatch):
    module = _script_module()
    monkeypatch.setattr(sys, "argv", ["migrate_working_to_threads.py", "--apply"])
    monkeypatch.setattr(module, "get_db", lambda: pytest.fail("must not open DB for --apply"))

    with pytest.raises(SystemExit) as raised:
        module.main()
    assert raised.value.code == 2
