"""A committed roster change reaches the owner's open clients (corsia#912)."""

from __future__ import annotations

import pytest


@pytest.fixture
def plain_thread(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES ('t1','user','New Chat','native/test','t','t')"
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def frames(monkeypatch):
    sent: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "infra.state.broadcast_event",
        lambda thread_id, user_id, message, exclude_ws=None: sent.append(
            (thread_id, user_id, message)
        ),
    )
    return sent


def test_promote_and_demote_publish_the_committed_roster_fields(plain_thread, frames):
    from core.agent_roster_mutations import change_agent

    change_agent(
        "t1", {"agent_enabled": True, "responsibility": "Owns docs."}, user_id="user",
    )
    (thread_id, user_id, promoted), = frames
    assert (thread_id, user_id) == ("t1", "user")
    assert promoted["type"] == "session_updated"
    assert promoted["thread_id"] == "t1"
    assert promoted["agent_enabled"] == 1
    assert promoted["agent_responsibility"] == "Owns docs."
    assert promoted["agent_status"] == "active"
    assert promoted["updated_at"] != "t"

    change_agent("t1", {"agent_enabled": False}, user_id="user")
    demoted = frames[-1][2]
    assert demoted["agent_enabled"] == 0
    assert demoted["agent_responsibility"] == "Owns docs."


def test_unchanged_retry_publishes_nothing(plain_thread, frames):
    from core.agent_roster_mutations import change_agent

    body = {"agent_enabled": True, "responsibility": "Owns docs."}
    change_agent("t1", body, user_id="user")
    change_agent("t1", body, user_id="user")
    assert len(frames) == 1


def test_delivery_failure_keeps_the_committed_change(plain_thread, monkeypatch):
    from core.agent_roster_mutations import change_agent
    from infra.database import get_db

    def boom(*_args, **_kwargs):
        raise RuntimeError("socket gone")

    monkeypatch.setattr("infra.state.broadcast_event", boom)
    result = change_agent(
        "t1", {"agent_enabled": True, "responsibility": "Owns docs."}, user_id="user",
    )
    assert result["changed"] is True
    db = get_db()
    try:
        assert db.execute(
            "SELECT agent_enabled FROM threads WHERE id='t1'"
        ).fetchone()[0] == 1
    finally:
        db.close()
