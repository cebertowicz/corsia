"""Direct naming seam for the private CC thread-row lookup."""

from __future__ import annotations

import sqlite3

from subagents import cc_runtime_resume
from subagents.cc_runtime_resume import _thread_row


def test_thread_row_reads_known_thread_and_rejects_unknown_id() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            parent_thread_id TEXT,
            project_id TEXT,
            title TEXT,
            spawned_by TEXT,
            model TEXT,
            user_id TEXT,
            runtime_context_epoch INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO threads (
            id, parent_thread_id, project_id, title, spawned_by, model,
            user_id, runtime_context_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "thread-1",
            "lineage-1",
            "project-1",
            "Thread title",
            "parent-1",
            "claude-code/sonnet",
            "user-1",
            7,
        ),
    )

    row = _thread_row("thread-1", db)

    assert row is not None
    # A CC launch never reads ``parent_thread_id``: ``spawned_by`` owns both the
    # thread's role and its parent broadcast address (issue #1617).
    assert dict(row) == {
        "project_id": "project-1",
        "title": "Thread title",
        "spawned_by": "parent-1",
        "model": "claude-code/sonnet",
        "user_id": "user-1",
        "runtime_context_epoch": 7,
    }
    assert _thread_row("missing", db) is None
    assert not hasattr(cc_runtime_resume, "_session_row")
