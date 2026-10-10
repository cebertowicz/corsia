"""Only actual checkpoint/cap changes release a conserved FIFO input.

A checkpoint refusal conserves input only while a preparing model switch still
owes it a transfer; every other refusal now fails the input visibly, so these
tests establish that switch before settling. The release machinery they cover
is unchanged.
"""

from __future__ import annotations

import pytest

from core.checkpoint_types import CheckpointRequest, CheckpointUnavailable
from infra.database import get_db
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input, finish_turn


@pytest.fixture
def blocked_input(isolated_db):
    db = get_db()
    db.execute(
        "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) "
        "VALUES ('local/held','local','held','local/held','Held',100000,'t0','t0')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('held-thread','user-1','local/held','t0','t0')"
    )
    from infra.queries import insert_message

    insert_message(
        "held-thread", "assistant", "prior completed response",
        {"turn_unbound_reason": "legacy_import"},
        msg_id="prior-response", db=db,
    )
    db.commit()
    admission = admit_input(
        "held-thread", role="user", content="keep original",
        intent="apply_now", message_id="held-input", turn_id="held-turn",
        boot_epoch="boot", admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="held-attempt", started_at="t2", db=db)
    try:
        yield db
    finally:
        db.close()


def _preparing_switch(db, thread_id="held-thread", operation_id="switch-held"):
    """The one owner that still transfers an unwritten suffix on its own."""
    from infra.model_switches import request_switch

    return request_switch(
        thread_id, "native/destination", operation_id=operation_id,
        requested_at="t4", db=db,
    )


def _settle_refusal(db):
    """Settle the attempt the checkpoint refused, without choosing its fate."""
    settle_batch_write(
        "held-attempt", outcome="proved_no_write", db=db,
        error=CheckpointUnavailable(
            "required checkpoint unavailable", context_window=100_000,
            installed_checkpoint_id=None,
        ),
    )


def _hold(db):
    """Conserve the refused input: a preparing switch owes it a transfer."""
    _preparing_switch(db)
    _settle_refusal(db)


def _row(db, message_id="held-input"):
    return tuple(db.execute(
        "SELECT content,delivery_state,seq,write_outcome,active_turn_id,"
        "delivery_attempt_id FROM messages WHERE id=?", (message_id,),
    ).fetchone())


def test_hold_and_release_publish_strictly_newer_delivery_revisions(blocked_input):
    """A delayed pre-hold frame must not erase the newer blocked display."""
    from infra.database import write_transaction
    from infra.input_writes import release_checkpoint_inputs

    db = blocked_input

    def revision():
        return db.execute(
            "SELECT delivery_revision FROM messages WHERE id='held-input'",
        ).fetchone()[0]

    initial = revision()
    _hold(db)
    held = revision()
    assert held > initial
    _hold(db)
    assert revision() == held  # Duplicate settlement is not another transition.
    with write_transaction(db, scope="test_cap_change"):
        db.execute("UPDATE models SET context_window=150000 WHERE id='local/held'")
        assert release_checkpoint_inputs(db, "local/held") == ("held-thread",)
    assert revision() > held


def test_release_resolves_a_shared_transport_once_not_per_input(blocked_input):
    """A batch must not multiply catalog reads while holding the writer lock."""
    from infra.database import write_transaction
    from infra.input_writes import release_checkpoint_inputs

    db = blocked_input
    db.execute("UPDATE models SET litellm_model='transport/held' WHERE id='local/held'")
    db.execute("UPDATE threads SET model='transport/held' WHERE id='held-thread'")
    db.execute("UPDATE messages SET target_model='transport/held' WHERE thread_id='held-thread'")
    db.commit()
    _hold(db)
    statements = []
    db.set_trace_callback(statements.append)
    try:
        with write_transaction(db, scope="test_batched_recovery"):
            db.execute("UPDATE models SET context_window=150000 WHERE id='local/held'")
            assert release_checkpoint_inputs(db, "local/held") == ("held-thread",)
    finally:
        db.set_trace_callback(None)
    catalog_reads = [sql for sql in statements if sql.startswith("SELECT * FROM models")]
    assert len(catalog_reads) == 2  # One exact-ID lookup and one transport lookup.
    assert _row(db)[3:] == (None, None, None)


@pytest.mark.parametrize("thread_id", ("held-thread", None))
def test_release_queries_scan_only_checkpoint_held_inputs(blocked_input, thread_id):
    """Both install and cap-change release avoid unrelated message history."""
    from infra.database import write_transaction
    from infra.input_writes import release_checkpoint_inputs

    db = blocked_input
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,created_at,delivery_state) "
        "VALUES (?, 'held-thread', 'assistant', 'unrelated', 't0', 'delivered')",
        ((f"unrelated-{number}",) for number in range(2_000)),
    )
    db.commit()
    _hold(db)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        with write_transaction(db, scope="test_bounded_checkpoint_release"):
            assert release_checkpoint_inputs(
                db, "local/held", thread_id=thread_id,
            ) == ("held-thread",)
    finally:
        db.set_trace_callback(None)

    release_sql = [
        sql for sql in statements
        if sql.startswith("SELECT DISTINCT target_model FROM messages WHERE")
        or sql.startswith("UPDATE messages SET write_outcome=NULL")
    ]
    assert len(release_sql) == 2
    for sql in release_sql:
        detail = " ".join(
            str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert "idx_messages_checkpoint_hold" in detail


@pytest.mark.parametrize("intent", ("apply_now", "queue_after_turn"))
def test_held_input_projects_the_same_write_evidence_over_rest_and_websocket(blocked_input, intent):
    """Live and reloaded messages must explain the same durable hold."""
    from api.thread_message_rows import public_delivery_message
    from api.thread_paging import page_messages
    from infra.send_broadcast import broadcast_input_rows, read_input_broadcast_row

    db = blocked_input
    db.execute("UPDATE messages SET delivery_intent=? WHERE id='held-input'", (intent,))
    db.commit()
    _hold(db)
    row = db.execute("SELECT * FROM messages WHERE id='held-input'").fetchone()
    rest = public_delivery_message(row)
    page, _, _ = page_messages(db, "held-thread", None, None)
    hydrated = next(message for message in page if message["id"] == "held-input")
    events = []
    broadcast_input_rows(
        "held-thread", (read_input_broadcast_row("held-input", db=db),),
        broadcast=lambda _thread_id, payload: events.append(payload),
    )
    for projected in (rest, hydrated, events[0]["message"]):
        assert projected["write_outcome"] == "proved_no_write"
        assert projected["delivery_state"] == "queued"
        assert projected["active_turn_id"] is None
        assert projected["delivery_error"] == "required checkpoint unavailable"
        assert (projected.get("metadata") or {}).get("delivery_intent") == (
            "queue_after_turn" if intent == "queue_after_turn" else None
        )


@pytest.mark.parametrize("settled", (False, True))
def test_restart_keeps_checkpoint_held_input_ineligible(blocked_input, settled):
    """Startup cannot treat a known unwritten refusal as an ambiguous old write."""
    from infra.thread_recovery import recover_threads

    db = blocked_input
    # No switch owes this input a transfer, so the refusal fails it visibly and
    # startup must neither resend it nor reopen it as ambiguous.
    settle_batch_write(
        "held-attempt", outcome="proved_no_write", db=db,
        error=CheckpointUnavailable(
            "required checkpoint unavailable", context_window=100_000,
            installed_checkpoint_id=None,
        ),
    )
    if settled:
        finish_turn(
            "held-turn", outcome="error", ended_at="t3", successor_turn_id="unused",
            boot_epoch="boot", db=db,
        )
    before = _row(db)
    recovered = recover_threads(
        boot_epoch="new-boot", recovered_at="t4", successor_id=lambda _id: "new-turn", db=db,
    )
    assert recovered.batches == ()
    assert all(terminal.successor is None for terminal in recovered.settlements)
    assert before[1] == "failed" and before[0] == "keep original"
    assert _row(db) == before


def test_cancel_held_input_keeps_its_text_and_is_not_released(blocked_input):
    """Existing Cancel remains the exit and a later recovery cannot undo it."""
    from infra.database import write_transaction
    from infra.input_writes import release_checkpoint_inputs
    from infra.queued_input_settlement import cancel_queued_input

    db = blocked_input
    _hold(db)
    revision = db.execute(
        "SELECT delivery_revision FROM messages WHERE id='held-input'",
    ).fetchone()[0]
    cancelled = cancel_queued_input("held-thread", "held-input", revision=revision, db=db)
    assert cancelled["content"] == "keep original"
    assert cancelled["delivery_state"] == "cancelled"
    assert cancelled["seq"] is not None
    before_release = _row(db)
    with write_transaction(db, scope="test_later_cap_change"):
        release_checkpoint_inputs(db, "local/held")
    assert _row(db) == before_release


@pytest.mark.parametrize("write_outcome,state", (("accepted", "delivered"), ("ambiguous", "ambiguous")))
def test_later_checkpoint_failure_cannot_resend_a_possible_write(blocked_input, write_outcome, state):
    """A maintenance error cannot undo accepted or uncertain provider evidence."""
    db = blocked_input
    settle_batch_write("held-attempt", outcome=write_outcome, db=db)
    before = _row(db)
    terminal = finish_turn(
        "held-turn", outcome="error", ended_at="t3", successor_turn_id="unused",
        boot_epoch="boot", db=db, error=CheckpointUnavailable(
            "checkpoint unavailable", context_window=100_000, installed_checkpoint_id=None,
        ),
    )
    assert terminal.successor is None
    assert _row(db) == before
    assert before[1] == state
    assert before[2] is not None


def test_stop_disposition_wins_over_a_checkpoint_error(blocked_input):
    """An explicit cancellation before writing must not become a waiting input."""
    db = blocked_input
    settle_batch_write(
        "held-attempt", outcome="proved_no_write", no_write_state="cancelled", db=db,
        error=CheckpointUnavailable(
            "checkpoint unavailable", context_window=100_000, installed_checkpoint_id=None,
        ),
    )
    terminal = finish_turn(
        "held-turn", outcome="cancelled", ended_at="t3", successor_turn_id="unused",
        boot_epoch="boot", db=db,
    )
    assert terminal.successor is None
    assert _row(db)[:2] == ("keep original", "cancelled")
    assert _row(db)[2] is not None


def test_switch_retargets_proved_unwritten_source_to_the_destination(blocked_input):
    """A held source exits without rewriting or resending its original input."""
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.model_switches import activate_ready_switch, request_switch
    from infra.thread_inputs import start_idle_turn

    db = blocked_input
    # The switch must already be preparing when the refusal settles: that
    # pending transfer is the only owner a conserved input may wait for.
    request = request_switch(
        "held-thread", "native/destination", operation_id="switch-held",
        requested_at="t4", db=db,
    )
    settle_batch_write(
        "held-attempt", outcome="proved_no_write", db=db,
        error=CheckpointUnavailable(
            "required checkpoint unavailable", context_window=100_000,
            installed_checkpoint_id=None,
        ),
    )
    assert finish_turn(
        "held-turn", outcome="error", ended_at="t3", successor_turn_id="unused",
        boot_epoch="boot", db=db,
    ).successor is None
    # The source still owned an unsettled write when the switch was requested.
    assert request.waiting_for_source
    assert _row(db) == ("keep original", "queued", None, None, None, None)
    assert db.execute("SELECT model FROM threads WHERE id='held-thread'").fetchone()[0] == "local/held"
    after = accept_thread_input(
        "held-thread", role="user", content="destination input", intent="apply_now",
        message_id="after-switch", db=db,
    )
    assert (after.target_model, after.target_execution_epoch) == ("native/destination", 1)
    with write_transaction(db, scope="test_held_switch_commit") as connection:
        connection.execute(
            "UPDATE threads SET switch_status='ready' "
            "WHERE switch_operation_id='switch-held'",
        )
        ready = activate_ready_switch(
            connection, "held-thread", committed_at="t5",
        )
    assert ready is not None and ready.committed
    successor = start_idle_turn(
        "held-thread", turn_id="after-switch-turn", boot_epoch="boot",
        started_at="t5", db=db,
    )
    assert successor is not None
    assert successor.target_model == "native/destination"
    assert [item.message_id for item in successor.items if item.role == "user"] == ["held-input", "after-switch"]
    assert tuple(db.execute(
        "SELECT target_model,target_execution_epoch FROM messages WHERE id='held-input'",
    ).fetchone()) == ("native/destination", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates,release", (
    ({"context_window": 150_000}, True),
    ({"context_window": 100_000}, False),
    ({"context_window": None}, False),
    ({"temperature": 0.2}, False),
))
@pytest.mark.parametrize("change_first", (False, True))
async def test_profile_change_and_hold_cannot_lose_each_other(
    blocked_input, monkeypatch, updates, release, change_first,
):
    """Changing a real effective cap releases once, regardless of race order."""
    from api import models
    from infra import thread_execution

    db = blocked_input

    async def no_provider_dispatch(thread_id, **_kwargs):
        # The external provider is outside this storage test. A post-commit wake
        # must already see the eligible input, not an uncommitted profile change.
        assert thread_id == "held-thread"
        assert _row(db)[3:] == (None, None, None)

    monkeypatch.setattr(thread_execution, "drive_thread", no_provider_dispatch)
    if not change_first:
        _hold(db)
    await models.update_model_profile("local/held", updates)
    if change_first:
        _hold(db)

    assert _row(db) == (
        "keep original", "queued", None,
        None if release else "proved_no_write", None,
        None if release else "held-attempt",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change_first", (False, True))
async def test_profile_cap_releases_a_transport_named_thread(
    blocked_input, monkeypatch, change_first,
):
    """Profile IDs and persisted transport names resolve to the same model."""
    from api import models
    from infra import thread_execution

    db = blocked_input
    db.execute("UPDATE models SET litellm_model='transport/held' WHERE id='local/held'")
    db.execute("UPDATE threads SET model='transport/held' WHERE id='held-thread'")
    db.execute("UPDATE messages SET target_model='transport/held' WHERE thread_id='held-thread'")
    db.commit()

    async def no_provider_dispatch(_thread_id, **_kwargs):
        return None

    monkeypatch.setattr(thread_execution, "drive_thread", no_provider_dispatch)
    if not change_first:
        _hold(db)
    await models.update_model_profile("local/held", {"context_window": 150_000})
    if change_first:
        _hold(db)
    assert _row(db)[3:] == (None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_sibling", (False, True))
async def test_profile_wake_failure_cannot_strand_another_released_input(
    blocked_input, monkeypatch, wait_for_sibling,
):
    """Independent released FIFOs run even when the first wake fails or waits."""
    import asyncio

    from api import models
    from infra import thread_execution
    from infra.thread_inputs import start_idle_turn

    db = blocked_input
    _hold(db)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('other-thread','user-1','local/held','t0','t0')",
    )
    db.commit()
    admission = admit_input(
        "other-thread", role="user", content="second saved input", intent="apply_now",
        message_id="other-input", turn_id="other-turn", boot_epoch="boot", admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="other-attempt", started_at="t2", db=db)
    _preparing_switch(db, "other-thread")
    settle_batch_write(
        "other-attempt", outcome="proved_no_write", db=db,
        error=CheckpointUnavailable("required checkpoint unavailable", context_window=100_000, installed_checkpoint_id=None),
    )
    # Each hold outlived the switch that owed it a transfer, which is the state
    # a cap change must still rescue; nothing else would ever release it.
    db.execute(
        "UPDATE threads SET switch_status=NULL,pending_model=NULL,"
        "pending_execution_epoch=NULL,switch_after_input_order=NULL"
    )
    db.commit()
    finish_turn("other-turn", outcome="error", ended_at="t3", successor_turn_id="unused", boot_epoch="boot", db=db)
    finish_turn("held-turn", outcome="error", ended_at="t3", successor_turn_id="unused", boot_epoch="boot", db=db)
    sibling_delivered = asyncio.Event()
    refused = RuntimeError("first wake failed before claim")

    async def drive_with_failure(thread_id):
        if thread_id == "held-thread":
            if wait_for_sibling:
                await asyncio.wait_for(sibling_delivered.wait(), timeout=1)
            raise refused
        # Stand in for provider I/O; admission, release, claim and settlement
        # use real owners and storage. The assertion below is durable delivery,
        # not merely that a mock driver was called.
        assert thread_id == "other-thread" and not db.in_transaction
        batch = start_idle_turn(thread_id, turn_id="recovered-turn", boot_epoch="boot", started_at="t4", db=db)
        assert batch is not None
        assert [item.message_id for item in batch.items if item.role == "user"] == ["other-input"]
        begin_batch_write(batch, attempt_id="recovered-attempt", started_at="t5", db=db)
        settle_batch_write("recovered-attempt", outcome="accepted", db=db)
        finish_turn("recovered-turn", outcome="completed", ended_at="t6", successor_turn_id="unused", boot_epoch="boot", db=db)
        sibling_delivered.set()

    monkeypatch.setattr(thread_execution, "drive_thread", drive_with_failure)
    with pytest.raises(Exception) as failure:
        await models.update_model_profile("local/held", {"context_window": 150_000})

    row = db.execute("SELECT content,delivery_state,write_outcome FROM messages WHERE id='other-input'").fetchone()
    assert tuple(row) == ("second saved input", "delivered", "accepted"), repr(failure.value)
    assert _row(db)[1:4] == ("queued", None, None)
    assert isinstance(failure.value, ExceptionGroup)
    assert failure.value.exceptions == (refused,), "the wake failure must not be swallowed"
    # A repeated profile value must not duplicate the successful delivery.
    assert (await models.update_model_profile("local/held", {"context_window": 150_000}))["ok"]
    assert db.execute("SELECT count(*) FROM messages WHERE id='other-input'").fetchone()[0] == 1


@pytest.mark.parametrize("change_first", (False, True))
@pytest.mark.parametrize("completion", ("installed", "failed", "overtaken"))
def test_checkpoint_completion_releases_only_an_actual_install(
    blocked_input, change_first, completion,
):
    """Failed or overtaken maintenance must never unlock the same input again."""
    from infra import checkpoints

    db = blocked_input
    boundary = db.execute(
        "SELECT id,seq FROM messages WHERE thread_id='held-thread' AND seq IS NOT NULL "
        "ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    checkpoints.create_running(CheckpointRequest(
        id="recovery-checkpoint", thread_id="held-thread", history_epoch=0,
        previous_generation_id=None, through_seq=boundary["seq"],
        through_message_id=boundary["id"], trigger="manual", mode="cold",
        source_harness="native", source_model="local/held", writer_harness="native",
        writer_model="local/held", originating_turn_id=None, first_included_seq=None,
        started_at="t3",
    ), db=db)
    if not change_first:
        _hold(db)
    if completion == "failed":
        checkpoints.fail("recovery-checkpoint", error="refused", ended_at="t4", db=db)
    else:
        checkpoints.capture(
            "recovery-checkpoint", raw_result="continuity", usage_json=None,
            captured_at="t4", db=db,
        )
        if completion == "overtaken":
            db.execute("UPDATE threads SET history_epoch=1 WHERE id='held-thread'")
            db.commit()
        assert checkpoints.install(
            "recovery-checkpoint", installed_at="t5", db=db,
        ).status == completion
    if change_first:
        _hold(db)

    release = completion == "installed"
    assert _row(db) == (
        "keep original", "queued", None,
        None if release else "proved_no_write", None,
        None if release else "held-attempt",
    )
    terminal = finish_turn(
        "held-turn", outcome="error", ended_at="t6", successor_turn_id="successor",
        boot_epoch="boot", db=db,
    )
    assert (terminal.successor is not None) is release
