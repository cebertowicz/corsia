from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


def _accept(
    thread_id: str,
    message_id: str,
    *,
    role: str,
    content: str,
    metadata: dict | None = None,
    intent: str = "apply_now",
) -> None:
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        thread_id,
        role=role,
        content=content,
        intent=intent,
        message_id=message_id,
        metadata=metadata,
        delivery_options={"native_mode": "event", "attachments": []},
        created_at="2026-08-04T00:00:00+00:00",
    )


@pytest.mark.parametrize("metadata,intent,expected", [
    (None, "apply_now", None),
    ({}, "apply_now", None),  # Admission stores empty metadata as SQL NULL.
    ({"delivery_intent": "queue_after_turn"}, "apply_now", {}),
    (None, "queue_after_turn", {"delivery_intent": "queue_after_turn"}),
    ({}, "queue_after_turn", {"delivery_intent": "queue_after_turn"}),
    ({"note": "kept"}, "queue_after_turn", {"note": "kept", "delivery_intent": "queue_after_turn"}),
])
def test_input_metadata_preserves_absence_and_canonical_queue_across_surfaces(
    isolated_db, metadata, intent, expected,
):
    """Queue projection must not invent metadata for an ordinary empty Send."""
    from api.thread_paging import page_messages
    from infra.database import get_db
    from infra.send_broadcast import broadcast_input_rows, read_input_broadcast_row
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("metadata-projection", model="native/test")
    _accept(thread_id, "metadata-input", role="user", content="preserved",
            metadata=metadata, intent=intent)
    db = get_db()
    try:
        row = read_input_broadcast_row("metadata-input", db=db)
        page, _, _ = page_messages(db, thread_id, None, None)
    finally:
        db.close()
    events = []
    broadcast_input_rows(thread_id, (row,), broadcast=lambda _thread, event: events.append(event))
    assert events[0]["message"]["metadata"] == expected
    assert next(item for item in page if item["id"] == "metadata-input")["metadata"] == expected


def test_system_input_broadcast_preserves_context_event_role(isolated_db):
    from infra.database import get_db
    from infra.send_broadcast import broadcast_input_rows, read_input_broadcast_row
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("system-input-broadcast", model="native/test")
    metadata = {
        "type": "context_event",
        "event_type": "system.restart.resume",
        "summary": "Instance restarted",
    }
    _accept(
        thread_id,
        "system-event-input",
        role="system",
        content="[EVENT: system.restart.resume]",
        metadata=metadata,
    )
    from infra.thread_inputs import start_idle_turn

    claimed = start_idle_turn(
        thread_id,
        turn_id="turn-system-input-broadcast",
        boot_epoch="boot-1",
        started_at="t1",
    )
    assert claimed is not None
    db = get_db()
    try:
        row = read_input_broadcast_row("system-event-input", db=db)
    finally:
        db.close()

    payloads: list[dict] = []
    assert row is not None
    broadcast_input_rows(
        thread_id,
        (row,),
        broadcast=lambda _thread_id, payload: payloads.append(payload),
    )

    message = payloads[0]["message"]
    assert message["role"] == "system"
    assert message["metadata"] == metadata
    assert message["delivery_state"] == "queued"
    assert message["seq"] is None
    assert message["active_turn_id"] == "turn-system-input-broadcast"


def test_singleton_broadcast_preserves_role_and_hides_private_metadata():
    from infra.send_broadcast import broadcast_input_row

    payloads: list[dict] = []
    broadcast_input_row(
        "system-singleton-thread",
        "system-singleton-event",
        "[EVENT: system.restart.resume]",
        {
            "type": "context_event",
            "runtime_context": "private",
            "_corsia_queued_delivery_options": {"private": True},
        },
        "2026-08-04T00:00:00+00:00",
        delivery_state="delivered",
        delivery_revision=2,
        seq=7,
        role="system",
        broadcast=lambda _thread_id, payload: payloads.append(payload),
    )

    message = payloads[0]["message"]
    assert message["role"] == "system"
    assert message["metadata"] == {"type": "context_event"}


def test_for_user_assistant_update_reaches_the_live_broadcast_path(isolated_db):
    """post_update's durable row must be readable for broadcast, not only after reload."""
    from infra.database import get_db
    from infra.queries import insert_message
    from infra.send_broadcast import broadcast_input_rows, read_input_broadcast_row
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("for-user-update-broadcast", model="native/test")
    db = get_db()
    try:
        message_id = insert_message(
            thread_id, "assistant", "The focused check passed.",
            {"for_user": True, "turn_id": "turn-1"}, db=db,
        )
        db.commit()
        row = read_input_broadcast_row(message_id, db=db)
    finally:
        db.close()

    assert row is not None, (
        "an explicit for_user agent update must be readable through the "
        "canonical broadcast projection, not silently dropped"
    )
    events: list[dict] = []
    broadcast_input_rows(thread_id, (row,), broadcast=lambda _t, e: events.append(e))
    assert events[0]["message"]["role"] == "assistant"
    assert events[0]["message"]["content"] == "The focused check passed."


def test_ordinary_assistant_row_is_never_an_input_broadcast(isolated_db):
    """The role guard exists so a plain assistant turn cannot become an input broadcast."""
    from infra.database import get_db
    from infra.queries import insert_message
    from infra.send_broadcast import read_input_broadcast_row
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("ordinary-assistant-row", model="native/test")
    db = get_db()
    try:
        no_marker = insert_message(
            thread_id, "assistant", "internal turn output",
            {"turn_id": "turn-1"}, db=db,
        )
        false_marker = insert_message(
            thread_id, "assistant", "internal turn output 2",
            {"turn_id": "turn-1", "for_user": False}, db=db,
        )
        db.commit()
        assert read_input_broadcast_row(no_marker, db=db) is None
        assert read_input_broadcast_row(false_marker, db=db) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_settled_batch_broadcast_reads_rows_then_publishes_in_order(
    isolated_db,
    monkeypatch,
):
    from infra import state
    from infra.send_broadcast import broadcast_message_rows
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("settled-batch-broadcast", model="native/test")
    _accept(thread_id, "event-first", role="system", content="event")
    _accept(thread_id, "user-second", role="user", content="continue")
    payloads: list[dict] = []
    monkeypatch.setattr(
        state,
        "broadcast_to_thread",
        lambda _thread_id, payload: payloads.append(payload),
    )

    await broadcast_message_rows(
        thread_id,
        ("event-first", "user-second"),
    )

    assert payloads[0]["type"] == "messages_added"
    assert [row["id"] for row in payloads[0]["messages"]] == [
        "event-first",
        "user-second",
    ]
    assert [row["role"] for row in payloads[0]["messages"]] == [
        "system",
        "user",
    ]


def test_a_claim_after_an_error_publishes_the_stamped_error_row(isolated_db, monkeypatch):
    """A live client learns `recovered` from the batch's own message_added, not a reload."""
    import asyncio

    import infra.state as state
    from infra.database import get_db
    from infra.send_broadcast import broadcast_claimed_batch
    from infra.thread_driver import admit_input

    published: list[dict] = []
    monkeypatch.setattr(state, "broadcast_to_thread", lambda _thread, payload, **_kw: published.append(payload))
    with get_db() as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-err','user-1','claude-code/sonnet','t0','t0')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES ('err-1','thread-err','system','boom','{\"type\":\"llm_error\"}','t0',1)"
        )
        db.commit()
        admitted = admit_input(
            "thread-err", role="system", content="wake", intent="apply_now",
            message_id="wake-1", metadata={"type": "context_event"}, turn_id="turn-err",
            boot_epoch="test-boot", admitted_at="t1", db=db,
        )
    batch = admitted.batch
    assert batch is not None and batch.recovered_message_ids == ("err-1",)

    asyncio.run(broadcast_claimed_batch(batch))

    order = [m["id"] for p in published for m in (p.get("messages") or [p["message"]])]
    assert order.index("err-1") < order.index("wake-1")
    sent = {m["id"]: m for p in published for m in (p.get("messages") or [p["message"]])}
    assert {"wake-1", "err-1"} <= set(sent)
    assert sent["err-1"]["metadata"]["recovered"] is True
