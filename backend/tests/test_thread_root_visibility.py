"""Visible-root lineage: attributing a hidden subagent's running Turn to its row.

The chat list hides every thread with ``spawned_by`` set, so a client cannot
map a subagent Turn back to the visible row that owns its lineage. The server
names that row — the top of the ``spawned_by`` chain — on the auth snapshot's
active turns and on ``turn_activity`` frames. These tests hold the chain walk,
the snapshot field, and the frame emission of that contract.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from core.thread_identity import root_visible_thread_id, root_visible_thread_ids
from core.turn_activity import _started_at_or_none, turn_activity_frame
from core.turn_core import TurnIdentity, active_turns_for_owner
from infra.database import get_db


def _seed_threads(threads, turns=()) -> None:
    """Insert (thread_id, spawned_by) rows and active turns for user-1."""
    db = get_db()
    try:
        for thread_id, spawned_by in threads:
            db.execute(
                "INSERT INTO threads (id, user_id, model, created_at, updated_at, spawned_by) "
                "VALUES (?, 'user-1', 'oMLX/qwen', 't', 't', ?)",
                (thread_id, spawned_by),
            )
        for turn_order, turn_id, thread_id in turns:
            db.execute(
                "INSERT INTO active_turns "
                "(turn_order, id, thread_id, execution_epoch, model, status, started_at, boot_epoch) "
                "VALUES (?, ?, ?, 0, 'oMLX/qwen', 'running', 't', 'test-boot')",
                (turn_order, turn_id, thread_id),
            )
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    ("chain", "thread_id", "expected"),
    [
        # A visible thread is its own root.
        ([("root", None)], "root", "root"),
        # A subagent of a plain visible chat thread resolves to the chat.
        ([("chat", None), ("sub", "chat")], "sub", "chat"),
        # Multi-level nesting walks to the top, not the immediate parent.
        (
            [("top", None), ("middle", "top"), ("leaf", "middle")],
            "leaf",
            "top",
        ),
    ],
)
def test_root_visible_thread_id_walks_the_spawn_chain(isolated_db, chain, thread_id, expected):
    _seed_threads(chain)

    assert root_visible_thread_id(thread_id) == expected


def test_batch_roots_match_single_root_resolution(isolated_db):
    _seed_threads([
        ("root", None),
        ("child", "root"),
        ("orphan", "missing-parent"),
        ("cycle-a", "cycle-b"),
        ("cycle-b", "cycle-a"),
    ])
    thread_ids = ["root", "child", "orphan", "cycle-a", "missing"]

    assert root_visible_thread_ids(thread_ids) == {
        thread_id: root_visible_thread_id(thread_id) for thread_id in thread_ids
    }


def test_root_visible_thread_id_is_none_for_a_broken_chain(isolated_db):
    _seed_threads([("orphan", "missing-parent")])

    assert root_visible_thread_id("orphan") is None
    assert root_visible_thread_id("never-created") is None


def test_root_visible_thread_id_is_none_for_a_cycle(isolated_db):
    _seed_threads([("a", "b"), ("b", "a")])

    assert root_visible_thread_id("a") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("user_id", "user-2"), ("project_id", "other-project")],
)
def test_root_visible_thread_id_is_none_for_a_cross_scope_ancestor(
    isolated_db, field, value,
):
    _seed_threads([("parent", None), ("sub", "parent")])
    db = get_db()
    try:
        if field == "project_id":
            db.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) "
                "VALUES ('other-project', 'Other', 't', 't')",
            )
        db.execute(f"UPDATE threads SET {field}=? WHERE id='parent'", (value,))
        db.commit()
    finally:
        db.close()

    assert root_visible_thread_id("sub") is None


def test_active_turns_for_owner_names_each_turns_visible_root(isolated_db):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('stranger', 'user-2', 'oMLX/qwen', 't', 't')",
        )
        db.commit()
    finally:
        db.close()
    _seed_threads(
        [("chat", None), ("sub-cc", "chat")],
        turns=[
            (1, "turn-own", "chat"),
            (2, "turn-sub", "sub-cc"),
            (3, "turn-other", "stranger"),
        ],
    )

    turns = active_turns_for_owner("user-1")

    assert turns == [
        {
            "thread_id": "chat",
            "turn_id": "turn-own",
            "turn_order": 1,
            "visible_root_thread_id": "chat",
            "started_at": "t",
        },
        {
            "thread_id": "sub-cc",
            "turn_id": "turn-sub",
            "turn_order": 2,
            "visible_root_thread_id": "chat",
            "started_at": "t",
        },
    ]


@pytest.mark.asyncio
async def test_turn_activity_frame_resolves_and_degrades(isolated_db, monkeypatch):
    _seed_threads([("chat", None), ("sub-cc", "chat")], turns=[(5, "turn-sub", "sub-cc")])
    identity = TurnIdentity("turn-sub", 5)

    assert await turn_activity_frame("sub-cc", identity) == {
        "type": "turn_activity",
        "thread_id": "sub-cc",
        "visible_root_thread_id": "chat",
        "started_at": "t",
        "turn_id": "turn-sub",
        "turn_order": 5,
    }

    # Display facts may never fail turn admission: a broken root lookup
    # degrades that field to None, and a Turn with no active_turns row has
    # no start to name.
    def _broken(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.thread_identity.root_visible_thread_id", _broken)
    frame = await turn_activity_frame("sub-cc", TurnIdentity("turn-unknown", 6))
    assert frame["visible_root_thread_id"] is None
    assert frame["started_at"] is None


def test_turn_start_lookup_degrades_to_none_on_a_database_error():
    """A failed start read logs and returns None; it never fails admission."""

    class _BrokenConnection:
        def execute(self, *_args):
            raise sqlite3.OperationalError("database is locked")

    assert _started_at_or_none("turn-sub", _BrokenConnection()) is None


def test_active_turns_for_owner_degrades_when_visible_root_lookup_raises(
    isolated_db, monkeypatch,
):
    _seed_threads([("chat", None)], turns=[(1, "turn-own", "chat")])

    def _broken(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.thread_identity.root_visible_thread_id", _broken)

    assert active_turns_for_owner("user-1") == [{
        "thread_id": "chat",
        "turn_id": "turn-own",
        "turn_order": 1,
        "visible_root_thread_id": None,
        "started_at": "t",
    }]


@pytest.mark.asyncio
async def test_cc_turn_activity_frame_names_the_visible_root(isolated_db, monkeypatch):
    """The CC acceptance broadcast carries the root and start with turn identity."""
    from types import SimpleNamespace

    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_accept

    _seed_threads([("parent-1", None), ("cc-child", "parent-1")], turns=[(4, "turn-cc", "cc-child")])
    identity = TurnIdentity("turn-cc", 4)
    frames: list[dict] = []

    async def admit(*_args, **_kwargs):
        return identity

    async def feed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cc_accept, "_admit_turn", admit)
    monkeypatch.setattr(cc_accept, "_prepare_live_turn", lambda *_args: None)
    monkeypatch.setattr(cc_accept, "_feed_frame", feed)

    live = SimpleNamespace(
        checkpoint_wait_scope=CheckpointWaitScope(),
        event_lock=asyncio.Lock(),
    )

    def bcast(thread_id, message):
        frames.append(message)

    accepted = await cc_accept.accept_reconstructed_input(
        "cc-child",
        live,
        SimpleNamespace(turn_text="input"),
        turn_id=identity.turn_id,
        bcast=bcast,
    )

    assert accepted == identity
    activity = [f for f in frames if f["type"] == "turn_activity"]
    assert activity == [{
        "type": "turn_activity",
        "thread_id": "cc-child",
        "visible_root_thread_id": "parent-1",
        "started_at": "t",
        "turn_id": "turn-cc",
        "turn_order": 4,
    }]


@pytest.mark.asyncio
async def test_codex_turn_activity_frame_names_the_visible_root(isolated_db):
    """The Codex live-turn start broadcast carries the same root contract."""
    from types import SimpleNamespace

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _seed_threads(
        [("parent-2", None), ("cx-child", "parent-2")],
        turns=[(6, "turn-cx", "cx-child")],
    )
    identity = TurnIdentity("turn-cx", 6)
    frames: list[dict] = []
    live = SimpleNamespace(session=None, corsia_turn=None)

    accepted = await codex_runtime._start_live_turn(
        "cx-child",
        live,
        "input",
        successor_turn_id="turn-cx",
        bcast=lambda thread_id, message: frames.append(message),
        preadmitted_turn=identity,
    )

    assert accepted == identity
    activity = [f for f in frames if f["type"] == "turn_activity"]
    assert activity == [{
        "type": "turn_activity",
        "thread_id": "cx-child",
        "visible_root_thread_id": "parent-2",
        "started_at": "t",
        "turn_id": "turn-cx",
        "turn_order": 6,
    }]
