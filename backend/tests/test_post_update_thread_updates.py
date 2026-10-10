"""``post_update`` records its message as the thread's update, atomically (update cards r2)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.thread_input_types import TurnIdentity

TURN = TurnIdentity(turn_id="turn-1", turn_order=1)


def _start_turn(db, turn_id: str) -> TurnIdentity:
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    admission = admit_input(
        "sender-self",
        role="user",
        content="Show the update footer.",
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch="test",
        admitted_at=utc_now_iso(),
        db=db,
    )
    assert admission.batch is not None
    turn_order = db.execute(
        "SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)
    ).fetchone()[0]
    return TurnIdentity(turn_id=turn_id, turn_order=int(turn_order))


def _message_id(result: str) -> str:
    return result.removeprefix("Update saved as message ").removesuffix(".")


def _seed_assistant_message(db, turn_id: str, metadata: dict) -> None:
    from infra.queries import insert_message

    insert_message(
        "sender-self", "assistant", "Sibling response",
        {"turn_id": turn_id, **metadata}, msg_id=f"{turn_id}-assistant", db=db,
    )


def _updates(db) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM thread_updates ORDER BY id")]


@pytest.fixture
def nudges(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr("tools.post_update.broadcast_message_rows", AsyncMock())
    monkeypatch.setattr(
        "infra.state.broadcast_to_user", lambda user_id, message: sent.append((user_id, message)),
    )
    return sent


@pytest.mark.asyncio
async def test_the_message_and_its_update_share_one_commit_and_timestamp(deleg_db, nudges):
    from infra import presence
    from tools.post_update import PostUpdateTool

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="The export shipped.", _host_access_turn_identity=TURN,
    )

    message = deleg_db.execute(
        "SELECT id, seq, created_at FROM messages WHERE thread_id='sender-self'"
    ).fetchone()
    assert message["id"] == _message_id(result)
    [update] = _updates(deleg_db)
    assert (update["kind"], update["source_kind"], update["source_id"]) == (
        "marked", "message", message["id"],
    )
    assert update["title"] is None and update["node_id"] is None
    assert update["after_seq"] == message["seq"]
    assert update["created_at"] == presence.iso(presence.parse(message["created_at"]))
    assert nudges == [("u-test", {"type": "thread_updates_changed", "thread_ids": ["sender-self"]})]


@pytest.mark.asyncio
async def test_update_copies_native_identity_from_its_running_turn_sibling(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    identity = _start_turn(deleg_db, "turn-with-native-sibling")
    _seed_assistant_message(deleg_db, identity.turn_id, {
        "model": "native/test", "model_name": "Native Test", "is_cloud": True,
    })

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="The model footer is available.",
        _host_access_turn_identity=identity,
    )

    [message] = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (_message_id(result),)
    ).fetchall()
    assert json.loads(message["metadata"]) == {
        "for_user": True, "turn_id": identity.turn_id,
        "model": "native/test", "model_name": "Native Test", "is_cloud": True,
    }


@pytest.mark.asyncio
async def test_update_copies_external_harness_identity_from_its_running_turn_sibling(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    identity = _start_turn(deleg_db, "turn-with-external-sibling")
    _seed_assistant_message(deleg_db, identity.turn_id, {
        "harness": "claude-code", "cc": True,
        "model": "claude-code/opus", "model_name": "Opus",
    })

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Keep the harness marker.",
        _host_access_turn_identity=identity,
    )

    [message] = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (_message_id(result),)
    ).fetchall()
    assert json.loads(message["metadata"]) == {
        "for_user": True, "turn_id": identity.turn_id,
        "harness": "claude-code", "cc": True,
        "model": "claude-code/opus", "model_name": "Opus",
    }


@pytest.mark.asyncio
async def test_update_uses_only_the_turn_model_when_sibling_model_is_not_usable(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    identity = _start_turn(deleg_db, "turn-with-incomplete-sibling")
    _seed_assistant_message(deleg_db, identity.turn_id, {
        "model": " \t", "model_name": "Sibling Label", "is_cloud": True,
        "harness": "codex", "codex": True,
    })

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Do not mix identities.",
        _host_access_turn_identity=identity,
    )

    [message] = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (_message_id(result),)
    ).fetchall()
    assert json.loads(message["metadata"]) == {
        "for_user": True, "turn_id": identity.turn_id,
        "model": "claude-code/opus",
    }


@pytest.mark.asyncio
async def test_update_uses_its_running_codex_turn_model_without_an_assistant_sibling(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    deleg_db.execute(
        "UPDATE threads SET model='codex/gpt-5-codex' WHERE id='sender-self'"
    )
    deleg_db.commit()
    identity = _start_turn(deleg_db, "turn-without-sibling")
    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Use the running model.",
        _host_access_turn_identity=identity,
    )

    [message] = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (_message_id(result),)
    ).fetchall()
    assert json.loads(message["metadata"]) == {
        "for_user": True, "turn_id": identity.turn_id,
        "model": "codex/gpt-5-codex",
    }


@pytest.mark.asyncio
async def test_update_does_not_copy_identity_from_a_different_turn(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    identity = _start_turn(deleg_db, "turn-with-other-sibling")
    _seed_assistant_message(deleg_db, "another-turn", {
        "model": "claude-code/opus", "model_name": "Opus", "cc": True,
    })

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Do not reuse another turn's identity.",
        _host_access_turn_identity=identity,
    )

    [message] = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (_message_id(result),)
    ).fetchall()
    assert json.loads(message["metadata"]) == {
        "for_user": True, "turn_id": identity.turn_id,
        "model": "claude-code/opus",
    }


@pytest.mark.asyncio
async def test_a_node_linked_send_keeps_the_node_but_no_title_or_text(deleg_db, nudges):
    from plans import service
    from tools.post_update import PostUpdateTool

    plan = service.ensure_plan("thread", "sender-self", db=deleg_db)
    node = service.add_node(plan.id, kind="task", title="Export", acceptance_criteria="done", db=deleg_db)
    deleg_db.commit()

    await PostUpdateTool().execute(
        _thread_id="sender-self", text="Raw detail stays in the message.", node_id=node.id,
        _host_access_turn_identity=TURN,
    )

    [update] = _updates(deleg_db)
    assert (update["node_id"], update["title"]) == (node.id, None)
    assert "Raw detail" not in repr(update)


@pytest.mark.asyncio
async def test_a_replay_adds_no_second_update_and_no_nudge(deleg_db, nudges):
    from tools.post_update import PostUpdateTool

    first = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Once", idempotency_key="k-1", _host_access_turn_identity=TURN,
    )
    again = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Once", idempotency_key="k-1", _host_access_turn_identity=TURN,
    )

    assert first == again
    assert [row["source_id"] for row in _updates(deleg_db)] == [_message_id(first)]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_a_failed_record_rolls_back_the_message_and_sends_no_nudge(deleg_db, nudges, monkeypatch):
    from tools.post_update import PostUpdateTool

    def _broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("infra.thread_updates.record_marked", _broken)

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Lost", _host_access_turn_identity=TURN,
    )

    assert result == "Error: update could not be saved (RuntimeError)"
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert _updates(deleg_db) == []
    assert nudges == []


@pytest.mark.asyncio
async def test_a_failed_nudge_leaves_the_committed_update(deleg_db, monkeypatch):
    from tools.post_update import PostUpdateTool

    monkeypatch.setattr("tools.post_update.broadcast_message_rows", AsyncMock())

    def _offline(*_args, **_kwargs):
        raise ConnectionError("socket gone")

    monkeypatch.setattr("infra.state.broadcast_to_user", _offline)

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Still saved", _host_access_turn_identity=TURN,
    )

    assert result.startswith("Update saved as message ")
    assert [row["source_id"] for row in _updates(deleg_db)] == [_message_id(result)]
