"""#504 — inbound sends render live in already-open clients (no refresh).

A human message typed into a CC / Codex / native main-chat thread from one
device must appear on a SECOND device already subscribed to the same thread,
without a visible self-duplicate on the sender.

The original bug: the CC and Codex main-chat WS handlers dispatched with
``broadcast_input=False`` (avoids the self-echo) but that ALSO skipped every
other subscribed client, so a second device saw nothing until refresh.

Under the durable-delivery protocol every canonical row state transition is
broadcast to all subscribed clients — including the sender, whose optimistic
row reconciles by message id / ``input_id`` upsert.  These tests therefore pin
one message identity per send plus the delivery-state contract fields, rather
than sender exclusion.

Also guards the spawn side (case B): a visible spawn broadcasts
``session_created`` to the owner's clients (sidebar), and a subagent spawn
broadcasts its spawn-chip ``message_added`` to the parent's viewers.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.usefixtures('signed_in_defaults')

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


@pytest.fixture
def ws_buckets():
    from infra.state import ws as ws_state

    sess = dict(ws_state._ws_thread_clients)
    usr = dict(ws_state._ws_user_clients)
    ws_state._ws_thread_clients.clear()
    ws_state._ws_user_clients.clear()
    try:
        yield ws_state
    finally:
        ws_state._ws_thread_clients.clear()
        ws_state._ws_thread_clients.update(sess)
        ws_state._ws_user_clients.clear()
        ws_state._ws_user_clients.update(usr)


def _seed_session(model: str, thread_id: str = "sA"):
    from infra import database

    db = database.get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, title, model, project_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (thread_id, "u1", "Target", model, None,
         "2026-07-01T00:00:00", "2026-07-01T00:00:00"),
    )
    db.commit()
    return dict(db.execute(
        "SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())


# --- Contract: every main-chat handler broadcasts to others, excludes sender ---

@pytest.mark.asyncio
async def test_cc_main_handler_broadcasts_input_excluding_sender():
    from ws import message_dispatch

    captured: dict = {}

    async def _fake_send(*args, **kwargs):
        captured.update(kwargs)

    sender_ws = _FakeWS()
    await message_dispatch.handle_cc_main_message(
        sender_ws, {"user_id": "u1", "title": "T", "model": "claude-code/opus"},
        "sA", "hello", [],
        msg_id="m1", now="2026-07-01T00:00:00",
        is_retry=False, is_edited=False,
        send_thread_input_fn=_fake_send,
        cc_attachment_block_fn=lambda _a, _u: "",
    )

    assert captured.get("broadcast_input") is True, "CC main must broadcast the inbound row cross-client"
    assert captured.get("broadcast_exclude_ws") is sender_ws, "CC main must exclude the sender's socket"


@pytest.mark.asyncio
async def test_codex_main_handler_broadcasts_input_excluding_sender():
    from ws import message_dispatch

    captured: dict = {}

    async def _fake_send(*args, **kwargs):
        captured.update(kwargs)

    sender_ws = _FakeWS()
    await message_dispatch.handle_codex_main_message(
        sender_ws, {"user_id": "u1", "title": "T", "model": "codex/gpt-5-codex"},
        "sA", "hello",
        msg_id="m1", now="2026-07-01T00:00:00", is_retry=False,
        send_thread_input_fn=_fake_send,
    )

    assert captured.get("broadcast_input") is True, "Codex main must broadcast the inbound row cross-client"
    assert captured.get("broadcast_exclude_ws") is sender_ws, "Codex main must exclude the sender's socket"


# --- Integration: real send_thread_input broadcast reaches Device B, not sender ---

@pytest.mark.parametrize("handler,model", [
    ("cc", "claude-code/opus"),
    ("codex", "codex/gpt-5-codex"),
    ("native", "opus"),
])
@pytest.mark.asyncio
async def test_inbound_send_reaches_second_device_not_sender(
    handler, model, isolated_db, ws_buckets, monkeypatch,
):
    from infra import send_service
    from shared_send_fixtures import _install_fake_native_context_agent
    from ws import message_dispatch

    _install_fake_native_context_agent(monkeypatch)
    session = _seed_session(model)

    # No real provider turn — we only assert the inbound message_added broadcast.
    async def _fake_dispatch(provider, ctx):
        await ctx.settle_delivery()
        return "mock", None, ctx.preadmitted_turn

    monkeypatch.setattr("infra.send_dispatch.dispatch", _fake_dispatch)

    sender = _FakeWS()   # Device A — typed the message (optimistic render)
    other = _FakeWS()    # Device B — already open on the same thread
    ws_buckets._ws_thread_clients["sA"] = [sender, other]
    ws_buckets._ws_user_clients["u1"] = [sender, other]

    real_send = send_service.send_thread_input
    if handler == "cc":
        await message_dispatch.handle_cc_main_message(
            sender, session, "sA", "hello", [],
            msg_id="m1", now="2026-07-01T00:00:00",
            is_retry=False, is_edited=False,
            send_thread_input_fn=real_send,
            cc_attachment_block_fn=lambda _a, _u: "",
        )
    elif handler == "codex":
        await message_dispatch.handle_codex_main_message(
            sender, session, "sA", "hello",
            msg_id="m1", now="2026-07-01T00:00:00", is_retry=False,
            send_thread_input_fn=real_send,
        )
    else:
        await real_send(
            "sA", session, "hello",
            user={"id": "u1"},
            persist_input=True, broadcast_input=True,
            native_mode="delegation", broadcast_exclude_ws=sender,
        )
    await asyncio.sleep(0.05)

    other_added = [m for m in other.sent if m.get("type") == "message_added"]
    other_types = [m.get("type") for m in other.sent]
    assert other_added, (
        f"[{handler}] Device B missed the inbound message_added; got {other_types}")

    # The durable-delivery protocol broadcasts canonical row state to every
    # client, including the sender: the sender's optimistic row reconciles by
    # message id / input_id upsert.  What must never happen is a second
    # distinct message identity (a visible duplicate) or an announcement that
    # skips the delivery-state contract fields.
    sender_added = [m for m in sender.sent if m.get("type") == "message_added"]
    sender_ids = {m["message"]["id"] for m in sender_added}
    other_ids = {m["message"]["id"] for m in other_added}
    other_messages = [event["message"] for event in other_added]
    subjects = [
        message for message in other_messages
        if message["role"] == "user" and message["content"] == "hello"
    ]
    digests = [
        message for message in other_messages
        if (message.get("metadata") or {}).get("event_type") == "context.digest"
    ]
    assert len(subjects) == 1, f"[{handler}] subject rows: {subjects}"
    assert len(digests) == 1, f"[{handler}] digest rows: {digests}"
    assert len(other_ids) == 2
    assert sender_ids <= other_ids, (
        f"[{handler}] sender saw a message identity other devices did not: "
        f"{sender_ids - other_ids}")
    for event in (*other_added, *sender_added):
        assert event["message"]["delivery_state"] in (
            "queued", "delivered",
        ), f"[{handler}] broadcast missed delivery-state contract: {event}"


# --- Case B guard: spawn visibility side effects reach open clients ---

@pytest.mark.asyncio
async def test_visible_spawn_emits_session_created_to_clients(
    isolated_db, ws_buckets,
):
    from infra import fresh_threads

    client = _FakeWS()
    ws_buckets._ws_user_clients["u1"] = [client]

    await fresh_threads.create_visible_fresh_thread(
        user_id="u1", title="Spawned Chat",
        model="opus", project_id=None, thread_kind="chat",
    )
    await asyncio.sleep(0.05)

    created = [m for m in client.sent if m.get("type") == "session_created"]
    assert created, f"visible spawn did not emit session_created; got {[m.get('type') for m in client.sent]}"
    # A visible chat thread carries no spawned_by, so the sidebar filter keeps it.
    assert created[0]["session"].get("spawned_by") is None


@pytest.mark.asyncio
async def test_subagent_spawn_chip_reaches_parent_viewers(
    isolated_db, ws_buckets,
):
    from infra import fresh_threads

    parent = _seed_session("opus", thread_id="parent")
    assert parent  # parent row exists for the chip insert

    viewer = _FakeWS()  # viewing the parent thread
    ws_buckets._ws_thread_clients["parent"] = [viewer]

    await fresh_threads.create_visible_fresh_thread(
        user_id="u1", title="Worker",
        model="claude-code/opus", project_id=None,
        spawned_by="parent", thread_kind="subagent", emit_spawn_chip=True,
    )
    await asyncio.sleep(0.05)

    chips = [
        m for m in viewer.sent
        if m.get("type") == "message_added"
        and (m.get("message") or {}).get("metadata", {}).get("type") == "subagent_ref"
    ]
    assert chips, f"subagent spawn chip did not reach the parent's viewer; got {[m.get('type') for m in viewer.sent]}"
