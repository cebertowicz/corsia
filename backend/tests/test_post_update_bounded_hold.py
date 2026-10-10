"""``post_update`` keeps its write-lock hold bounded: transcript scans run before the lock."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.queries import insert_message
from infra.thread_input_types import TurnIdentity

TURN = TurnIdentity(turn_id="turn-1", turn_order=1)


def _message_id(result: str) -> str:
    return result.removeprefix("Update saved as message ").removesuffix(".")


def _own_node(db):
    from plans import service

    plan = service.ensure_plan("thread", "sender-self", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Current task", acceptance_criteria="Done", db=db,
    )
    db.commit()
    return node


def _running_turn(db, turn_id: str) -> TurnIdentity:
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    admit_input(
        "sender-self", role="user", content="Go.", intent="apply_now",
        message_id=f"{turn_id}-input", turn_id=turn_id, boot_epoch="test",
        admitted_at=utc_now_iso(), db=db,
    )
    order = db.execute("SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)).fetchone()[0]
    return TurnIdentity(turn_id=turn_id, turn_order=int(order))


@pytest.fixture
def tool(deleg_db, monkeypatch):
    from tools.post_update import PostUpdateTool

    monkeypatch.setattr("tools.post_update.broadcast_message_rows", AsyncMock())
    return PostUpdateTool()


def _statements_in_write_transaction(db, run) -> list[str]:
    """Statements the connection ran between BEGIN IMMEDIATE and COMMIT."""
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        run()
    finally:
        db.set_trace_callback(None)
    begin = next(i for i, sql in enumerate(seen) if sql.startswith("BEGIN IMMEDIATE"))
    return seen[begin + 1 : next(i for i, sql in enumerate(seen) if sql == "COMMIT")]


@pytest.mark.asyncio
async def test_transcript_lookups_under_the_write_lock_are_bounded_by_position(deleg_db, tool):
    turn = _running_turn(deleg_db, "turn-scan")
    insert_message("sender-self", "assistant", "Sibling", {"turn_id": "turn-scan"}, db=deleg_db)
    for n in range(5):
        insert_message("sender-self", "assistant", f"Filler {n}", {"turn_id": f"other-{n}"}, db=deleg_db)

    from tools.post_update import _persist_update

    locked = _statements_in_write_transaction(
        deleg_db, lambda: _persist_update("sender-self", "Hello", None, "key-1", turn.turn_id),
    )

    scans = [sql for sql in locked if "json_extract" in sql]
    assert len(scans) == 2, scans
    for sql in scans:
        detail = " ".join(
            row[-1] for row in deleg_db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        )
        assert "idx_messages_session_seq (thread_id=? AND seq>?)" in detail, detail


@pytest.mark.asyncio
async def test_first_write_returns_a_new_message_with_literal_metadata_and_marked_row(deleg_db, tool):
    node = _own_node(deleg_db)

    result = await tool.execute(
        _thread_id="sender-self", text="  Done.  ", node_id=node.id,
        idempotency_key="k1", _host_access_turn_identity=TURN,
    )

    message_id = _message_id(result)
    row = deleg_db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    assert (row["role"], row["content"], row["seq"]) == ("assistant", "Done.", 1)
    assert json.loads(row["metadata"]) == {
        "for_user": True, "turn_id": "turn-1", "node_id": node.id,
        "post_update_idempotency_key": "k1",
    }
    marked = [dict(r) for r in deleg_db.execute("SELECT * FROM thread_updates")]
    assert marked == [{
        "id": 1, "thread_id": "sender-self", "kind": "marked", "source_kind": "message",
        "source_id": message_id, "node_id": node.id, "title": None, "after_seq": 1,
        "created_at": row["created_at"],
    }]


@pytest.mark.asyncio
async def test_same_key_replay_returns_the_first_message_and_writes_nothing(deleg_db, tool):
    first = await tool.execute(
        _thread_id="sender-self", text="Once", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )
    second = await tool.execute(
        _thread_id="sender-self", text="Once", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )

    assert second == first
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert deleg_db.execute("SELECT COUNT(*) FROM thread_updates").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_same_key_with_different_text_is_refused(deleg_db, tool):
    await tool.execute(
        _thread_id="sender-self", text="Once", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )
    result = await tool.execute(
        _thread_id="sender-self", text="Other", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )

    assert result == "Error: idempotency_key was already used for a different update; pass a new key"
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unknown_node_is_refused_and_writes_nothing(deleg_db, tool):
    result = await tool.execute(
        _thread_id="sender-self", text="Hi", node_id="no-such-node",
        _host_access_turn_identity=TURN,
    )

    assert result == (
        "Error: node_id is not an open or settled node in your plan; "
        "omit it or pass a node from plan_read"
    )
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_same_key_write_committed_after_the_read_ahead_is_still_a_replay(
    deleg_db, tool, monkeypatch,
):
    import tools.post_update as module

    real = module._read_ahead

    def read_then_race(*args):
        ahead = real(*args)
        insert_message(
            "sender-self", "assistant", "Raced",
            {"for_user": True, "turn_id": "turn-1", "post_update_idempotency_key": "k1"},
            msg_id="raced", db=deleg_db,
        )
        return ahead

    monkeypatch.setattr(module, "_read_ahead", read_then_race)

    result = await tool.execute(
        _thread_id="sender-self", text="Raced", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )

    assert _message_id(result) == "raced"
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_deleted_read_ahead_match_falls_back_to_the_older_match(deleg_db, tool, monkeypatch):
    import tools.post_update as module

    for ident in ("older", "newer"):
        insert_message(
            "sender-self", "assistant", "Same",
            {"for_user": True, "turn_id": "turn-1", "post_update_idempotency_key": "k1"},
            msg_id=ident, db=deleg_db,
        )
    real = module._read_ahead

    def read_then_delete(*args):
        ahead = real(*args)
        assert ahead.prior_id == "newer"
        deleg_db.execute("DELETE FROM messages WHERE id='newer'")
        deleg_db.commit()
        return ahead

    monkeypatch.setattr(module, "_read_ahead", read_then_delete)

    result = await tool.execute(
        _thread_id="sender-self", text="Same", idempotency_key="k1",
        _host_access_turn_identity=TURN,
    )

    assert _message_id(result) == "older"


@pytest.mark.asyncio
async def test_running_turn_copies_the_identity_of_its_sibling_found_before_the_lock(deleg_db, tool):
    turn = _running_turn(deleg_db, "turn-a")
    insert_message(
        "sender-self", "assistant", "Sibling",
        {"turn_id": "turn-a", "model": "m-old", "harness": "codex"}, db=deleg_db,
    )

    result = await tool.execute(
        _thread_id="sender-self", text="Update", _host_access_turn_identity=turn,
    )

    metadata = json.loads(
        deleg_db.execute("SELECT metadata FROM messages WHERE id=?", (_message_id(result),)).fetchone()[0]
    )
    assert metadata == {"for_user": True, "turn_id": "turn-a", "model": "m-old", "harness": "codex"}


@pytest.mark.asyncio
async def test_a_sibling_committed_after_the_read_ahead_wins_as_the_newest(deleg_db, tool, monkeypatch):
    import tools.post_update as module

    turn = _running_turn(deleg_db, "turn-a")
    insert_message(
        "sender-self", "assistant", "Early", {"turn_id": "turn-a", "model": "m-old"}, db=deleg_db,
    )
    real = module._read_ahead

    def read_then_race(*args):
        ahead = real(*args)
        insert_message(
            "sender-self", "assistant", "Late", {"turn_id": "turn-a", "model": "m-new"}, db=deleg_db,
        )
        return ahead

    monkeypatch.setattr(module, "_read_ahead", read_then_race)

    result = await tool.execute(
        _thread_id="sender-self", text="Update", _host_access_turn_identity=turn,
    )

    metadata = json.loads(
        deleg_db.execute("SELECT metadata FROM messages WHERE id=?", (_message_id(result),)).fetchone()[0]
    )
    assert metadata["model"] == "m-new"


@pytest.mark.asyncio
async def test_a_turn_that_became_running_after_the_read_ahead_still_copies_its_sibling(
    deleg_db, tool, monkeypatch,
):
    import tools.post_update as module

    real = module._read_ahead
    holder: dict = {}

    def read_then_start(*args):
        ahead = real(*args)
        assert ahead.sibling_scanned is False
        holder["turn"] = _running_turn(deleg_db, "turn-late")
        insert_message(
            "sender-self", "assistant", "Sibling", {"turn_id": "turn-late", "model": "m-x"}, db=deleg_db,
        )
        return ahead

    monkeypatch.setattr(module, "_read_ahead", read_then_start)

    result = await tool.execute(
        _thread_id="sender-self", text="Update",
        _host_access_turn_identity=TurnIdentity(turn_id="turn-late", turn_order=1),
    )

    metadata = json.loads(
        deleg_db.execute("SELECT metadata FROM messages WHERE id=?", (_message_id(result),)).fetchone()[0]
    )
    assert metadata["model"] == "m-x"
