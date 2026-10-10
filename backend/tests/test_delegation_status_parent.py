"""#1949 — thread_status names a thread's parent so a worker opens beside it.

For a hidden worker ``spawned_by`` is the canonical parent (and the subagent
marker); ``parent_thread_id`` is visible-thread lineage only. Only the Claude
Code child path writes both, so the field must not read the raw column alone.
"""
from __future__ import annotations

import pytest

from delegation_fixtures import _TOKEN, client, deleg_db  # noqa: F401


@pytest.fixture(autouse=True)
def _not_live(monkeypatch):
    from subagents import cc_runtime
    monkeypatch.setattr(cc_runtime, "is_live", lambda sid: False)


def _seed(db, tid, *, user="u-test", spawned_by=None, parent=None):
    db.execute(
        "INSERT INTO threads "
        "(id, user_id, model, spawned_by, parent_thread_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'claude-code/sonnet', ?, ?, 'T', 'now', 'now')",
        (tid, user, spawned_by, parent),
    )
    db.commit()


def _status(client, tid):
    return client.get(
        f"/api/delegation/threads/{tid}/status",
        params={"token": _TOKEN, "messages": 0},
    )


class TestThreadStatusParent:
    def test_cc_child_with_both_columns_returns_parent(self, client, deleg_db):
        _seed(deleg_db, "cc-child", spawned_by="sender-self", parent="sender-self")
        body = _status(client, "cc-child").json()
        assert body["thread_kind"] == "subagent"
        assert body["parent_thread_id"] == "sender-self"

    def test_non_cc_child_with_spawned_by_only_returns_parent(self, client, deleg_db):
        _seed(deleg_db, "codex-child", spawned_by="sender-self")
        body = _status(client, "codex-child").json()
        assert body["thread_kind"] == "subagent"
        assert body["parent_thread_id"] == "sender-self"

    def test_lineage_chat_returns_its_lineage_parent(self, client, deleg_db):
        _seed(deleg_db, "coordinator", parent="sender-self")
        body = _status(client, "coordinator").json()
        assert body["thread_kind"] == "chat"
        assert body["parent_thread_id"] == "sender-self"

    def test_top_level_thread_returns_null(self, client, deleg_db):
        body = _status(client, "sender-self").json()
        assert body["thread_kind"] == "chat"
        assert body["parent_thread_id"] is None
        # Additive: the existing keys are all still there.
        for key in ("thread_id", "title", "turn_active", "active_turn", "recent_messages"):
            assert key in body

    def test_other_users_thread_is_404(self, client, deleg_db):
        _seed(deleg_db, "foreign-child", user="u-other", spawned_by="sender-self")
        assert _status(client, "foreign-child").status_code == 404
