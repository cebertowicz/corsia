from __future__ import annotations

import json
import sqlite3


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    return db


def test_workspace_context_settings_ignore_the_legacy_key():
    from core.workspace_context_sources import workspace_context_configured_files

    db = _db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("system_context", json.dumps({"bootloader_files": ["context/legacy.md"]})),
    )
    assert "context/legacy.md" not in workspace_context_configured_files(db=db)

    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("workspace_context", json.dumps({"bootloader_files": ["context/current.md"]})),
    )
    assert workspace_context_configured_files(db=db) == ["context/current.md"]


def test_migration_copies_legacy_setting_and_keeps_the_old_row():
    from infra.database_migrations import _migrate_43_to_44

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    db.execute("INSERT INTO settings VALUES ('system_context', '{\"a\": 1}', 't')")
    _migrate_43_to_44(db)
    _migrate_43_to_44(db)
    rows = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert rows == {"system_context": '{"a": 1}', "workspace_context": '{"a": 1}'}


def test_legacy_snapshot_rows_render_through_the_workspace_origin():
    from core.installed_context import installed_parts, restore_sources

    stored = [
        {
            "source_id": "system-bootstrap:context/A.md",
            "origin_section": "system",
            "label": "System context: context/A.md",
            "stable_reference": "/w/context/A.md",
            "freshness": "fresh",
            "inclusion": "included",
            "reason": "system bootstrap file",
            "owner_surface": "document-viewer",
            "prompt_content": "## context/A.md\n\nbytes",
        },
        {
            "source_id": "cc-seed",
            "origin_section": "system",
            "label": "seed",
            "stable_reference": "seed",
            "freshness": "fresh",
            "inclusion": "included",
            "reason": "seed",
            "owner_surface": "runtime",
            "prompt_content": "seed",
        },
    ]
    rows = restore_sources(stored)
    assert installed_parts(rows, "workspace") == ["## context/A.md\n\nbytes"]
    assert installed_parts(rows, "system") == ["seed"]
    assert stored[0]["origin_section"] == "system"
