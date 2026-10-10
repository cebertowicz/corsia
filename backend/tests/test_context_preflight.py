from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


def test_preflight_scans_only_dispatch_memory_window(monkeypatch):
    from core.context_admission import check_context_preflight

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute("INSERT INTO threads VALUES (?, ?)", ("s1", None))
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("old-protected", "s1", "assistant", "old protected event", '{"type":"event"}', "000"),
    )
    for i in range(100):
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"recent-{i}", "s1", "assistant", f"recent {i}", None, f"{i + 1:03d}"),
        )
    conn.execute("UPDATE messages SET seq = rowid")

    result = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
    )

    with_old_row = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
        memory_window=101,
    )

    assert result.status == "allowed"
    assert result.history_tokens < with_old_row.history_tokens


def test_preflight_blocks_long_history_without_prepared_checkpoint(monkeypatch):
    from core.context_admission import check_context_preflight

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 1500,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute("INSERT INTO threads VALUES (?, ?)", ("s1", None))
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dense-1", "s1", "assistant", "x" * 900, None, "001"),
    )

    result = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
    )

    assert result.status == "blocked"
    assert result.continuity_mode == "blocked"
    assert "no prepared checkpoint-tail payload was supplied" in (
        result.block_reason or ""
    )


def test_preflight_counts_dispatch_prepared_history_when_supplied(monkeypatch):
    from core.context_admission import check_context_preflight
    from core.token_estimation import _estimate_messages_tokens

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(installed=None, project_cwd=None, agent_facts={}, sources=[]),
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute("INSERT INTO threads VALUES (?, ?)", ("s1", None))
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("raw-1", "s1", "assistant", "x" * 60000, None, "001"),
    )
    prepared_history = [
        {"role": "assistant", "content": "dispatch prepared answer"},
        {"role": "user", "content": "dispatch prepared follow-up"},
    ]

    result = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
        prepared_history=prepared_history,
    )

    assert result.status == "allowed"
    assert result.history_tokens == _estimate_messages_tokens(prepared_history)
    assert result.history_tokens < 100


def test_preflight_includes_role_sources_in_fit_projection(monkeypatch):
    from core.context_admission import _prompt_context_sources
    from core.effective_context import ContextSourceEntry

    role_source = ContextSourceEntry(
        source_id="role-context:workspace:workspace:agent:context/roles/agent/AGENT.md",
        origin_section="role",
        label="Agent role context",
        stable_reference="/workspace/context/roles/agent/AGENT.md",
        freshness="fresh",
        inclusion="included",
        reason="selected Agent role",
        owner_surface="document-viewer",
        prompt_content="ROLE FIT PAYLOAD",
        metadata={"provider": "role_context", "role": "agent"},
    )
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            installed=None, project_cwd=None,
            agent_facts={},
            sources=[role_source],
        ),
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT)")
    conn.execute("INSERT INTO threads VALUES (?, ?)", ("agent", None))

    assert _prompt_context_sources("agent", conn) == [role_source]


def test_preflight_blocks_when_project_sources_cannot_be_inspected(monkeypatch):
    from core.context_admission import check_context_preflight

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )

    def fail_project_sources(*_args, **_kwargs):
        raise sqlite3.OperationalError("project source table missing")

    monkeypatch.setattr(
        "core.project_context.project_context_source_entries_and_cwd",
        fail_project_sources,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO threads VALUES (?, ?)",
        ("s1", "project-a"),
    )

    result = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
    )

    assert result.status == "blocked"
    assert result.continuity_mode == "blocked"
    assert "project source lookup failed" in (result.block_reason or "")


def test_preflight_blocks_required_source_overflow_without_prepared_checkpoint(
    monkeypatch,
):
    from core.context_admission import check_context_preflight
    from core.effective_context import ContextSourceEntry

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 1500,
    )

    def fake_collect_context_sources(**_kwargs):
        return SimpleNamespace(
            installed=None, project_cwd=None,
            agent_facts={},
            sources=[
                ContextSourceEntry(
                    source_id="role:agent-owner-map",
                    origin_section="role",
                    label="Agent responsibility map",
                    stable_reference="thread://s1/agents",
                    freshness="fresh",
                    inclusion="included",
                    reason="stable responsibility map",
                    owner_surface="agents",
                    prompt_content="MUST_RETAIN_AGENT_FACT " * 300,
                )
            ],
        )

    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        fake_collect_context_sources,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO threads VALUES (?, ?)",
        ("s1", None),
    )

    result = check_context_preflight(
        thread_id="s1",
        model="anthropic/test",
        current_user_content="go",
        db=conn,
    )

    assert result.status == "blocked"
    assert result.continuity_mode == "blocked"
    assert result.history_tokens == 0
    assert result.source_tokens > 0
    assert result.source_section_tokens["role"] == result.source_tokens
    assert "no prepared checkpoint-tail payload was supplied" in (
        result.block_reason or ""
    )


def test_native_preflight_fails_closed_when_model_window_is_unknown(monkeypatch):
    from core.context_admission import check_context_preflight
    from llm.context_window import ModelContextWindowUnknown

    def unknown_window(_model):
        raise ModelContextWindowUnknown("catalog has no measured window")

    monkeypatch.setattr("llm.capabilities.get_model_context_window", unknown_window)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(installed=None, project_cwd=None, agent_facts={}, sources=[]),
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER
        );
        """
    )
    conn.execute("INSERT INTO threads VALUES (?, ?)", ("s-unknown-window", None))

    result = check_context_preflight(
        thread_id="s-unknown-window",
        model="openai/not-discovered",
        current_user_content="go",
        db=conn,
    )

    assert result.status == "blocked"
    assert result.model_context_window == 0
    assert "catalog has no measured window" in (result.block_reason or "")
