"""Guard the approved Connected Apps import contract at its public seams."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools import mcp_import


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _db() -> sqlite3.Connection:
    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _project(db: sqlite3.Connection, project_id: str, path: Path) -> None:
    db.execute(
        "INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        (project_id, project_id, str(path), "t", "t"),
    )
    db.commit()


def test_detection_reads_only_claude_project_mcp_servers_and_collapses_duplicates(tmp_path: Path) -> None:
    one = tmp_path / "work" / "one"
    two = tmp_path / "work" / "two"
    _json(tmp_path / ".claude.json", {
        "projects": {
            str(one): {"mcpServers": {"Shared": {"type": "http", "url": "https://example.test/mcp"}}},
            str(two): {"mcpServers": {"Shared again": {"type": "http", "url": "https://example.test/mcp"}}},
        },
        "other": {"mcpServers": {"must-not-read": {"type": "http", "url": "https://bad.test"}}},
        "unrelated": {"url": "https://also-bad.test", "credential": "not-a-server"},
    })

    detected = mcp_import.detected_sources(tmp_path)

    assert detected == [{
        "source": "claude_code", "app": "Claude Code",
        "servers": [{"name": "Shared", "kind": "remote", "folder": "one"}],
    }]
    rendered = json.dumps(detected)
    assert str(one) not in rendered
    assert "must-not-read" not in rendered and "not-a-server" not in rendered


def test_declared_claude_websocket_is_detected_but_cannot_be_imported(tmp_path: Path) -> None:
    _json(tmp_path / ".claude.json", {
        "mcpServers": {"Realtime": {"type": "ws", "url": "wss://example.test/mcp"}},
    })
    db = _db()
    try:
        assert mcp_import.detected_sources(tmp_path) == [{
            "source": "claude_code", "app": "Claude Code",
            "servers": [{"name": "Realtime", "kind": "unsupported"}],
        }]

        imported = mcp_import.import_selected(
            [{"source": "claude_code", "name": "Realtime"}], db=db, home=tmp_path, actor="agent",
        )

        assert imported == {"rows": [{"name": "Realtime", "status": "cannot_connect"}]}
        assert db.execute("SELECT count(*) FROM connected_apps").fetchone()[0] == 0
    finally:
        db.close()


def test_trailing_claude_project_folder_import_stays_in_its_project(tmp_path: Path) -> None:
    from api.connected_app_rows import app_reaches_project
    from connections.app_reach import get_app

    db = _db()
    try:
        project_a = tmp_path / "a"
        project_b = tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()
        _project(db, "a", project_a)
        _project(db, "b", project_b)
        _json(tmp_path / ".claude.json", {
            "projects": {str(project_b) + "/": {"mcpServers": {"Scoped": {"command": "npx"}}}},
        })

        imported = mcp_import.import_selected(
            [{"source": "claude_code", "name": "Scoped"}], db=db, home=tmp_path, actor="agent",
        )
        app = get_app(imported["rows"][0]["id"], db=db)

        assert app["reach"] == {"mode": "selected", "project_ids": ["b"], "outside_projects": False}
        assert app_reaches_project(app, "b")
        assert not app_reaches_project(app, "a")
    finally:
        db.close()


def test_import_status_copy_is_exact_and_never_mentions_bearer_file() -> None:
    assert mcp_import.IMPORT_NONE_FOUND == (
        "I didn't find any apps to bring over from Claude Code, Claude Desktop, Cursor or Codex."
    )
    assert mcp_import.IMPORT_UNSUPPORTED == "CORSIA can't connect to that kind of app yet."
    assert mcp_import.IMPORT_FOLDER_OUTSIDE_PROJECT == (
        "To use it in a project, connect it from that project's settings."
    )
    assert mcp_import.IMPORT_KEY_STAYS_IN_CODEX == (
        "Its key stays in Codex, so you'll set it in Connected apps after it comes over."
    )
    assert all("bearer_file" not in line for line in (
        mcp_import.IMPORT_NONE_FOUND,
        mcp_import.IMPORT_UNSUPPORTED,
        mcp_import.IMPORT_FOLDER_OUTSIDE_PROJECT,
        mcp_import.IMPORT_KEY_STAYS_IN_CODEX,
    ))


def test_imported_header_key_is_marked_set_for_the_approval_projection(tmp_path: Path) -> None:
    from api.connected_app_rows import server_row
    from connections.app_reach import get_app

    _json(tmp_path / ".cursor/mcp.json", {
        "mcpServers": {"GitHub": {
            "type": "http", "url": "https://example.test/mcp",
            "headers": {"Authorization": "import-secret"},
        }},
    })
    db = _db()
    try:
        imported = mcp_import.import_selected(
            [{"source": "cursor", "name": "GitHub"}], db=db, home=tmp_path, actor="agent",
        )
        app = get_app(imported["rows"][0]["id"], db=db)

        assert server_row(app, on_here=True, full=True, db=db)["key_refs"] == [{
            "kind": "header", "name": "Authorization", "set": True,
        }]
        assert "import-secret" not in json.dumps(app)
    finally:
        db.close()


def test_legacy_settings_runtime_launchers_remain_absent() -> None:
    from tools import mcp_runtime

    assert not hasattr(mcp_runtime, "start_mcp_runtime")
    assert not hasattr(mcp_runtime, "reload_mcp_runtime")


def test_key_reference_descriptors_reject_non_env_or_header_kind() -> None:
    from connections.app_reach import AppReachError, key_ref_descriptors

    db = _db()
    try:
        with pytest.raises(AppReachError):
            key_ref_descriptors("app", {"key_refs": [{"kind": "runtime_app_config", "name": "bad"}]}, db=db)
    finally:
        db.close()


def test_empty_stored_header_value_does_not_change_safe_text() -> None:
    from tools.mcp import _StoredHeadersAuth

    assert _StoredHeadersAuth({"X-Empty": ""}, "https://example.test").redact("safe") == "safe"
