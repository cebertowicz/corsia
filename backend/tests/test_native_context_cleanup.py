from __future__ import annotations

import json
import sqlite3
from contextlib import closing


class _NoClose:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def test_project_model_context_uses_only_context_directory(tmp_path, monkeypatch):
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(
        "infra.config.WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="workspace-123", root=tmp_path.resolve(), name="Primary"),
        raising=False,
    )
    from core import project_context
    from infra import database

    project_dir = tmp_path / "projects" / "demo"
    (project_dir / "context").mkdir(parents=True)
    (project_dir / "context" / "PROJECT.md").write_text(
        "CURRENT_PROJECT_TRUTH", encoding="utf-8"
    )
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text(
        "CURRENT_COORDINATION_TRUTH", encoding="utf-8"
    )
    (project_dir / "context" / "LIVE.md").write_text(
        "CURRENT_ADDITIONAL_TRUTH", encoding="utf-8"
    )
    (project_dir / "PROJECT.md").write_text(
        "STALE_ROOT_PROJECT", encoding="utf-8"
    )
    (project_dir / "BRIEF.md").write_text("STALE_ROOT_BRIEF", encoding="utf-8")
    (project_dir / "working").mkdir()
    (project_dir / "working" / "scratch.md").write_text(
        "PRIVATE_WORKING_FILE", encoding="utf-8"
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, "
        "bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES (?, ?, ?, ?)",
        (
            "p1",
            "Demo",
            "workspace:/" "/workspace-123/projects/demo",
            json.dumps(["PROJECT.md", "BRIEF.md", "working/scratch.md"]),
        ),
    )
    conn.commit()
    monkeypatch.setattr(database, "get_db", lambda: _NoClose(conn))

    rendered = project_context.load_project_context(tmp_path, "p1")
    sources = project_context.project_context_source_entries(tmp_path, "p1")

    assert rendered is not None
    assert "CURRENT_PROJECT_TRUTH" in rendered
    assert "CURRENT_COORDINATION_TRUTH" in rendered
    assert "CURRENT_ADDITIONAL_TRUTH" in rendered
    assert "STALE_ROOT_PROJECT" not in rendered
    assert "STALE_ROOT_BRIEF" not in rendered
    assert "PRIVATE_WORKING_FILE" not in rendered
    assert "Working Files" not in rendered
    assert [source.metadata["relative_path"] for source in sources] == [
        "context/PROJECT.md",
        "context/LIVE.md",
        "context/COORDINATION-MODEL.md",
    ]


def test_native_system_prompt_does_not_query_recent_kb_or_embed_policy(
    tmp_path, monkeypatch
):
    from core.context_builder import ContextBuilder
    from memory import knowledge_base

    def fail_if_queried(*_args, **_kwargs):
        raise AssertionError("queryless turns must not read recent KB entries")

    monkeypatch.setattr(knowledge_base, "get_recent_entries", fail_if_queried)

    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "CORSIA" in prompt
    assert str(tmp_path.resolve()) in prompt
    assert "You are Jarvis" not in prompt
    assert "Tool Call Guidelines" not in prompt
    assert "Memory (3 layers)" not in prompt
    assert "Permanent facts about Steve" not in prompt


def test_native_history_rebuild_uses_only_message_history(isolated_db):
    from core.thread_history import rebuild_messages_from_connection
    from infra.database import get_db

    thread_id = "current"
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO threads(id,user_id,created_at,updated_at) VALUES (?, 'owner','t','t')",
            (thread_id,),
        )
        for index in range(12):
            role = "user" if index % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO messages(id,thread_id,role,content,seq,created_at,delivery_state) "
                "VALUES (?,?,?,?,?,'t','delivered')",
                (f"m{index}", thread_id, role, f"history {index}", index + 1),
            )

        conn.commit()
        rebuilt = rebuild_messages_from_connection(thread_id, 4, db=conn)

    serialized = json.dumps(rebuilt)
    assert "history 11" in serialized

    assert "Previous conversation context" not in serialized
    assert "Understood. I have context" not in serialized
