"""Producer-owned due work for idle checkpoint admission."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import signed_in_default

# Native-harness threads idle-fold warm from their Turn's receipt or skip; a
# shared-local-model thread stays excluded at the producer, like a child or
# archived thread.
CC_MODEL = "claude-code/test"
EXTERNAL_MODEL = "external/test"
LOCAL_MODEL = "omlx@test"


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) VALUES "
        "(?,'claude-code','anthropic',?,?,?,200000,'t0','t0')",
        (CC_MODEL,) * 4,
    )
    for model_id, provider in ((EXTERNAL_MODEL, "external"), (LOCAL_MODEL, "omlx")):
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, 200000, 't0', 't0')",
            (model_id, "native", provider, model_id, model_id, model_id),
        )
    from connections.models import upsert_connection

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"}, db=db)
    db.commit()
    signed_in_default("claude-code", db=db)  # an input is admitted only on a signed-in account
    return db


def _thread(db: sqlite3.Connection, thread_id: str, model: str = "native/test") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,'t0','t0')",
        (thread_id, "user", model),
    )
    db.commit()


def _turn(
    db: sqlite3.Connection,
    thread_id: str,
    turn_id: str,
    *,
    status: str = "completed",
    ended_at: str | None = "2026-01-01T00:00:00",
    pending: int = 0,
    model: str = "native/test",
) -> None:
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,"
        " boot_epoch,idle_checkpoint_pending) VALUES (?,?,0,?,?,'t0',?,'boot',?)",
        (turn_id, thread_id, model, status, ended_at, pending),
    )
    db.commit()


def _message(db: sqlite3.Connection, thread_id: str, message_id: str = "m1") -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES (?,?,'assistant','done','t1',1)",
        (message_id, thread_id),
    )
    db.commit()


def _request(thread_id: str, turn_id: str, checkpoint_id: str = "cp-1"):
    from core.checkpoint_types import CheckpointRequest

    return CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=1,
        through_message_id="m1",
        trigger="idle",
        mode="warm",
        source_harness="native",
        source_model="native/test",
        writer_harness="native",
        writer_model="native/test",
        originating_turn_id=turn_id,
        first_included_seq=None,
        started_at="t2",
    )


def test_idle_target_refuses_only_unavailable_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.checkpoint_types import IdleCheckpointReason
    from llm import resolution
    from thread import idle_checkpoint

    def unavailable(*_args, **_kwargs):
        raise resolution.ModelUnavailableError("missing", "not connected")

    monkeypatch.setattr(resolution, "resolve_execution_target", unavailable)
    skipped = idle_checkpoint._resolved_target(object(), "missing", "thread-1")
    assert skipped.reason is IdleCheckpointReason.TARGET_UNAVAILABLE

    def malformed(*_args, **_kwargs):
        raise ValueError("invalid model metadata")

    monkeypatch.setattr(resolution, "resolve_execution_target", malformed)
    with pytest.raises(ValueError, match="invalid model metadata"):
        idle_checkpoint._resolved_target(object(), "broken", "thread-1")


def test_fresh_schema_has_checked_pending_marker_and_due_index() -> None:
    db = _db()
    try:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(active_turns)")}
        assert columns["idle_checkpoint_pending"][3] == 1
        assert columns["idle_checkpoint_pending"][4] == "0"
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_active_turns_idle_checkpoint_due'"
        ).fetchone()[0]
        assert "ended_at" in sql and "turn_order" in sql
        assert "WHERE idle_checkpoint_pending=1" in sql
        idle_index = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='checkpoints_idle_boundary'"
        ).fetchone()[0]
        assert "UNIQUE INDEX" not in idle_index
        assert "WHERE trigger='idle'" in idle_index
        with pytest.raises(sqlite3.IntegrityError):
            _thread(db, "bad")
            _turn(db, "bad", "bad-turn", pending=2)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (("completed", 1), ("cancelled", 0), ("error", 0)),
)
def test_finish_turn_arms_only_an_idle_completed_turn(outcome: str, expected: int) -> None:
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _thread(db, "thread-1", model=CC_MODEL)
        _turn(db, "thread-1", "turn-1", status="running", ended_at=None, model=CC_MODEL)
        finish_turn(
            "turn-1",
            outcome=outcome,
            ended_at="2026-01-01T00:00:00",
            successor_turn_id="turn-2",
            boot_epoch="boot",
            db=db,
        )
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == expected
    finally:
        db.close()


def test_successor_turn_prevents_completed_turn_from_becoming_due() -> None:
    from infra.input_admission import accept_thread_input
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="running", ended_at=None)
        accept_thread_input(
            "thread-1",
            role="user",
            content="next",
            intent="queue_after_turn",
            message_id="next",
            db=db,
        )
        result = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="2026-01-01T00:00:00",
            successor_turn_id="turn-2",
            boot_epoch="boot",
            db=db,
        )
        assert result.successor is not None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_a_waking_input_invalidates_a_prior_idle_marker() -> None:
    """The marker is cleared by the Turn that takes over, not by acceptance."""
    from infra.thread_driver import admit_input

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="completed", ended_at="t1", pending=1)

        admission = admit_input(
            "thread-1",
            role="system",
            content="event wake",
            intent="apply_now",
            message_id="event-1",
            turn_id="turn-2",
            boot_epoch="boot",
            admitted_at="t2",
            db=db,
        )

        assert not admission.accepted.duplicate
        assert admission.batch is not None, "the waking input should start a Turn"
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_a_passive_context_event_preserves_a_prior_idle_marker() -> None:
    """A passive event wakes nothing, so it must not drop the warm fold."""
    import json

    from infra.thread_driver import admit_input

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="completed", ended_at="t1", pending=1)

        admission = admit_input(
            "thread-1",
            role="system",
            content="a document changed",
            intent="queue_after_turn",
            message_id="event-passive",
            turn_id="turn-2",
            boot_epoch="boot",
            admitted_at="t2",
            metadata=json.loads(
                '{"type":"context_event","event_disposition":"passive"}'
            ),
            db=db,
        )

        assert admission.batch is None, "a passive event must not start a Turn"
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_duplicate_input_replay_preserves_a_later_idle_marker() -> None:
    from infra.thread_driver import admit_input

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1")
        first = admit_input(
            "thread-1",
            role="system",
            content="event wake",
            intent="apply_now",
            message_id="event-1",
            turn_id="turn-2",
            boot_epoch="boot",
            admitted_at="t2",
            db=db,
        )
        db.execute(
            "UPDATE active_turns SET idle_checkpoint_pending=1 WHERE id='turn-1'"
        )

        replay = admit_input(
            "thread-1",
            role="system",
            content="event wake",
            intent="apply_now",
            message_id="event-1",
            turn_id="turn-3",
            boot_epoch="boot",
            admitted_at="t3",
            db=db,
        )

        assert not first.accepted.duplicate and replay.accepted.duplicate
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE id='event-1'"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_a_failed_turn_start_rolls_the_marker_clear_back() -> None:
    """The clear and the Turn that justifies it stand or fall together."""
    from infra.thread_driver import admit_input

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(
            db, "thread-1", "turn-1", status="completed", ended_at="t1", pending=1,
        )
        db.execute(
            "CREATE TRIGGER reject_new_turn "
            "BEFORE INSERT ON active_turns "
            "BEGIN SELECT RAISE(ABORT,'injected acceptance failure'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected acceptance failure"):
            admit_input(
                "thread-1",
                role="system",
                content="event wake",
                intent="apply_now",
                message_id="event-1",
                turn_id="turn-2",
                boot_epoch="boot",
                admitted_at="t2",
                db=db,
            )

        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE id='event-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_direct_turn_start_invalidates_a_prior_idle_marker() -> None:
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-old", pending=1)
        accept_thread_input(
            "thread-1",
            role="user",
            content="Subagent finished",
            intent="apply_now",
            message_id="parent-completion",
            db=db,
        )
        batch = start_idle_turn(
            "thread-1",
            turn_id="turn-new",
            boot_epoch="boot",
            started_at="2026-01-02T00:00:00",
            db=db,
        )
        assert batch is not None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-old'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_model_switch_completion_does_not_arm_wrong_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="running", ended_at=None)
        monkeypatch.setattr(
            "infra.model_switches.activate_ready_switch", lambda *args, **kwargs: object()
        )
        result = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="2026-01-01T00:00:00",
            successor_turn_id="unused",
            boot_epoch="boot",
            db=db,
        )
        assert result.switch_transition is not None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_idle_context_refuses_when_turn_source_model_drifted() -> None:
    from core.checkpoint_types import IdleCheckpointReason, IdleCheckpointSkip
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread.idle_checkpoint import read_idle_context

    db = _db()
    try:
        _thread(db, "thread-1", model="native/current")
        _turn(
            db,
            "thread-1",
            "turn-1",
            pending=1,
            model="native/prior",
        )
        assert read_idle_context(
            IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00"),
            db=db,
        ) == IdleCheckpointSkip(IdleCheckpointReason.STALE)
    finally:
        db.close()


def test_new_input_invalidates_old_marker_and_later_completion_rearms() -> None:
    from infra.thread_driver import admit_input, finish_turn

    db = _db()
    try:
        _thread(db, "thread-1", model=CC_MODEL)
        _turn(db, "thread-1", "turn-old", pending=1, model=CC_MODEL)
        admitted = admit_input(
            "thread-1",
            role="user",
            content="again",
            intent="apply_now",
            message_id="input-2",
            turn_id="turn-new",
            boot_epoch="boot",
            admitted_at="2026-01-02T00:00:00",
            db=db,
        )
        assert admitted.batch is not None
        from infra.input_writes import begin_batch_write, settle_batch_write

        begin_batch_write(
            admitted.batch, attempt_id="write-new", started_at="2026-01-02T00:00:01", db=db
        )
        settle_batch_write("write-new", outcome="accepted", db=db)
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-old'"
        ).fetchone()[0] == 0
        finish_turn(
            "turn-new",
            outcome="completed",
            ended_at="2026-01-02T00:01:00",
            successor_turn_id="unused",
            boot_epoch="boot",
            db=db,
        )
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-new'"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_store_consumes_marker_and_creates_idle_row_atomically() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")
        result = consume_idle_marker(
            "turn-1", _request("thread-1", "turn-1"), db=db
        )
        assert result.disposition is IdleCheckpointDisposition.ADMITTED
        assert result.reason is IdleCheckpointReason.FIRED
        assert result.record is not None and result.record.id == "cp-1"
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    finally:
        db.close()


def test_store_consumes_marker_and_records_failed_idle_row_atomically() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition
    from infra.idle_checkpoints import consume_idle_marker_failure

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")

        result = consume_idle_marker_failure(
            "turn-1",
            _request("thread-1", "turn-1"),
            error="warm source exploded",
            ended_at="t3",
            db=db,
        )

        assert result.disposition is IdleCheckpointDisposition.FAILED
        assert result.record is not None
        assert result.record.status == "failed"
        assert result.record.error == "warm source exploded"
        assert result.record.ended_at == "t3"
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    finally:
        db.close()


def test_store_consumes_preparation_failure_without_checkpoint_provenance() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.idle_checkpoints import consume_idle_marker_failure

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)

        result = consume_idle_marker_failure(
            "turn-1",
            None,
            error="invalid model metadata",
            ended_at="t3",
            db=db,
        )
        repeated = consume_idle_marker_failure(
            "turn-1",
            None,
            error="invalid model metadata",
            ended_at="t4",
            db=db,
        )

        assert result.disposition is IdleCheckpointDisposition.FAILED
        assert result.reason is IdleCheckpointReason.FAILED
        assert result.record is None
        assert repeated.disposition is IdleCheckpointDisposition.STALE
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_failed_idle_row_and_marker_consumption_roll_back_together() -> None:
    from infra.idle_checkpoints import consume_idle_marker_failure

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")
        db.execute(
            "CREATE TRIGGER reject_failed_idle BEFORE UPDATE OF status ON checkpoints "
            "WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            consume_idle_marker_failure(
                "turn-1",
                _request("thread-1", "turn-1"),
                error="warm source exploded",
                ended_at="t3",
                db=db,
            )

        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_prior_idle_boundary_consumes_rearmed_marker_without_another_call() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.idle_checkpoints import (
        consume_idle_marker,
        consume_idle_marker_failure,
    )

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")
        first = consume_idle_marker(
            "turn-1", _request("thread-1", "turn-1"), db=db
        )
        assert first.disposition is IdleCheckpointDisposition.ADMITTED
        db.execute(
            "UPDATE checkpoints SET status='failed',ended_at='t3' WHERE id='cp-1'"
        )
        db.execute(
            "UPDATE active_turns SET idle_checkpoint_pending=1 WHERE id='turn-1'"
        )

        second = consume_idle_marker_failure(
            "turn-1",
            _request("thread-1", "turn-1", checkpoint_id="cp-2"),
            error="second failure",
            ended_at="t4",
            db=db,
        )

        assert second.disposition is IdleCheckpointDisposition.REFUSED
        assert second.reason is IdleCheckpointReason.ALREADY_COVERED
        assert second.record is None
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    finally:
        db.close()


def test_input_winning_the_race_makes_store_return_typed_stale() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition
    from infra.idle_checkpoints import consume_idle_marker_failure
    from infra.thread_driver import admit_input

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")
        admit_input(
            "thread-1",
            role="user",
            content="race",
            intent="apply_now",
            message_id="input-2",
            turn_id="turn-2",
            boot_epoch="boot",
            admitted_at="2026-01-02T00:00:00",
            db=db,
        )
        result = consume_idle_marker_failure(
            "turn-1",
            _request("thread-1", "turn-1"),
            error="lost race",
            ended_at="t3",
            db=db,
        )
        assert result.disposition is IdleCheckpointDisposition.STALE
        assert result.record is None
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_running_turn_makes_store_consume_and_refuse_old_marker() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-old", pending=1)
        _turn(
            db,
            "thread-1",
            "turn-live",
            status="running",
            ended_at=None,
        )
        _message(db, "thread-1")
        result = consume_idle_marker(
            "turn-old", _request("thread-1", "turn-old"), db=db
        )
        assert result.disposition is IdleCheckpointDisposition.REFUSED
        assert result.reason is IdleCheckpointReason.TURN_RUNNING
        assert result.record is None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-old'"
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_store_consumes_an_explicit_refusal_without_creating_a_row() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition
    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        result = consume_idle_marker("turn-1", None, db=db)
        assert result.disposition is IdleCheckpointDisposition.REFUSED
        assert result.record is None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cancelled_runtime_launches_a_committed_idle_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from core import checkpoint_runtime
    from core.checkpoint_types import CheckpointProviderForkRequest
    from core.checkpoint_types import (
    IdleCheckpointAdmissionResult,
    IdleCheckpointDisposition,
    IdleCheckpointReason,
)
    from llm.resolution import ExecutionTarget

    request = _request("thread-1", "turn-1")
    target = ExecutionTarget(
        catalog_id="native/test",
        harness="native",
        provider="native",
        backend="local",
        transport_model="wire",
        expected_reported_model=None,
        context_window=262_144,
        available=True,
        availability_reason=None,
        display_name="native",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )
    provider = CheckpointProviderForkRequest(
        fork_id=request.id,
        thread_id=request.thread_id,
        target=target,
        instruction="checkpoint",
        reasoning_effort=None,
        working_dir="/work",
        warm_source=object(),
    )
    admission = SimpleNamespace(request=request, provider_request=provider)
    result = IdleCheckpointAdmissionResult(
        IdleCheckpointDisposition.ADMITTED,
        SimpleNamespace(id=request.id),
        reason=IdleCheckpointReason.FIRED,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    launched = []

    async def delayed_run(*args, **kwargs):
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(checkpoint_runtime, "run_in_db", delayed_run)
    monkeypatch.setattr(
        checkpoint_runtime,
        "_launch_checkpoint",
        lambda candidate, source: launched.append((candidate, source)),
    )

    task = asyncio.create_task(
        checkpoint_runtime.request_idle_checkpoint("turn-1", admission)
    )
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert launched == [(request, provider)]


@pytest.mark.asyncio
async def test_runtime_broadcasts_failed_idle_row_without_launching_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import checkpoint_runtime
    from core.checkpoint_types import (
        IdleCheckpointAdmissionResult,
        IdleCheckpointDisposition,
        IdleCheckpointReason,
    )

    request = _request("thread-1", "turn-1")
    record = SimpleNamespace(thread_id="thread-1", status="failed")
    result = IdleCheckpointAdmissionResult(
        IdleCheckpointDisposition.FAILED, record, reason=IdleCheckpointReason.FAILED,
    )
    committed = []
    broadcasts: list[str] = []
    launched: list[object] = []

    async def immediate(function, *args, **kwargs):
        committed.append((function, args, kwargs))
        return result

    monkeypatch.setattr(checkpoint_runtime, "run_in_db", immediate)
    monkeypatch.setattr(checkpoint_runtime, "_broadcast_changed", broadcasts.append)
    monkeypatch.setattr(checkpoint_runtime, "_launch_checkpoint", launched.append)

    actual = await checkpoint_runtime.fail_idle_checkpoint(
        "turn-1", request, RuntimeError("warm source exploded")
    )

    assert actual is result
    assert committed[0][0].__name__ == "consume_idle_marker_failure"
    assert committed[0][1] == ("turn-1", request)
    assert committed[0][2]["error"] == "warm source exploded"
    assert broadcasts == ["thread-1"]
    assert launched == []


@pytest.mark.asyncio
async def test_cancelled_runtime_commits_preparation_failure_without_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from core import checkpoint_runtime
    from core.checkpoint_types import (
        IdleCheckpointAdmissionResult,
        IdleCheckpointDisposition,
        IdleCheckpointReason,
    )

    result = IdleCheckpointAdmissionResult(
        IdleCheckpointDisposition.FAILED, reason=IdleCheckpointReason.FAILED,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    committed = []
    broadcasts: list[str] = []

    async def delayed_run(function, *args, **kwargs):
        committed.append((function, args, kwargs))
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(checkpoint_runtime, "run_in_db", delayed_run)
    monkeypatch.setattr(checkpoint_runtime, "_broadcast_changed", broadcasts.append)

    task = asyncio.create_task(
        checkpoint_runtime.fail_idle_checkpoint(
            "turn-1", None, ValueError("invalid model metadata")
        )
    )
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert committed[0][0].__name__ == "consume_idle_marker_failure"
    assert committed[0][1] == ("turn-1", None)
    assert committed[0][2]["error"] == "invalid model metadata"
    assert broadcasts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_idle_preparation_uses_same_source_warm_capture(
    harness: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from history.types import HistoryBoundary
    from infra.idle_checkpoints import IdleCheckpointDue
    from llm.resolution import ExecutionTarget
    from thread import idle_checkpoint

    target = ExecutionTarget(
        catalog_id=f"catalog-{harness}",
        harness=harness,
        provider=harness,
        backend="harness",
        transport_model="wire",
        expected_reported_model=None,
        context_window=262_144,
        available=True,
        availability_reason=None,
        display_name=harness,
        supports_reasoning=True,
        preserve_thinking_default=True,
    )
    due = IdleCheckpointDue(
        "turn-1", 1, "thread-1", "2026-01-01T00:00:00"
    )
    context = SimpleNamespace(
        due=due,
        target=target,
        boundary=HistoryBoundary("thread-1", 0, 1, "m1"),
        previous=None,
        working_dir="/work",
        reasoning_effort="high",
        binary=harness,
    )

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    captured = []

    async def capture(thread_id, boundary, **kwargs):
        captured.append((thread_id, boundary, kwargs))
        return object()

    monkeypatch.setattr(idle_checkpoint, "run_in_db", immediate)
    monkeypatch.setattr(idle_checkpoint, "read_idle_context", lambda _due: context)
    monkeypatch.setattr(
        "core.fork_render.capture_warm_fork_source", capture
    )

    preparation = await idle_checkpoint.prepare_idle_checkpoint(due, "cp-1")

    assert preparation is not None
    assert preparation.request.mode == "warm"
    assert preparation.request.trigger == "idle"
    assert preparation.request.source_harness == harness
    assert captured == []

    admission = await idle_checkpoint.complete_idle_checkpoint(preparation)

    assert admission is not None
    assert admission.request is preparation.request
    assert admission.provider_request.cold_source is None
    assert captured[0][2]["target"] is target


def _native_receipt(
    final_message_id="m-120", provider_input_tokens=60_000, generation_id=None,
):
    from core.checkpoint_native_admission import NativeIdleReceipt

    return NativeIdleReceipt(
        messages=(
            {"role": "system", "content": "Live native prefix."},
            {"role": "user", "content": "The Turn's question."},
            {"role": "assistant", "content": "The Turn's answer."},
        ),
        tools=({"type": "function", "function": {"name": "read_file"}},),
        provider_input_tokens=provider_input_tokens,
        rendered_input_tokens=10,
        generation_id=generation_id,
        final_message_id=final_message_id,
    )


def _native_idle_context(due, *, context_window=262_144):
    from history.types import HistoryBoundary
    from llm.resolution import ExecutionTarget

    target = ExecutionTarget(
        catalog_id=EXTERNAL_MODEL,
        harness="native",
        provider="external",
        backend="network",
        transport_model="external@ns/model",
        expected_reported_model=None,
        context_window=context_window,
        available=True,
        availability_reason=None,
        display_name=EXTERNAL_MODEL,
        supports_reasoning=False,
    )
    return SimpleNamespace(
        due=due,
        target=target,
        boundary=HistoryBoundary("thread-1", 0, 120, "m-120"),
        previous=None,
        working_dir="/work",
        reasoning_effort=None,
        binary=None,
    )


@pytest.mark.asyncio
async def test_idle_native_fold_forks_its_receipt_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native idle fold resends the final call's prefix and adds one instruction.

    No cold correction rides along, and the generic harness capture never runs.
    """
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    from core.fork_native import NativeWarmSource, _messages_and_tools
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread import idle_checkpoint

    receipt = _native_receipt()
    due = IdleCheckpointDue(
        "turn-1", 1, "thread-1", "2026-01-01T00:00:00", native_receipt=receipt,
    )
    context = _native_idle_context(due)

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def capture(*_args, **_kwargs):
        raise AssertionError("a native idle fold never uses the harness capture")

    monkeypatch.setattr(idle_checkpoint, "run_in_db", immediate)
    monkeypatch.setattr(idle_checkpoint, "read_idle_context", lambda _due: context)
    monkeypatch.setattr("core.fork_render.capture_warm_fork_source", capture)

    preparation = await idle_checkpoint.prepare_idle_checkpoint(due, "cp-1")
    admission = await idle_checkpoint.complete_idle_checkpoint(preparation)

    assert admission.request is preparation.request
    assert admission.request.mode == "warm"
    assert admission.request.trigger == "idle"
    assert admission.request.writer_harness == "native"
    provider = admission.provider_request
    assert provider.cold_source is None
    assert provider.same_model_cold is None
    assert isinstance(provider.warm_source, NativeWarmSource)
    assert provider.warm_source.boundary == context.boundary
    assert provider.warm_source.prefix_messages == receipt.messages
    messages, tools, _seq = await _messages_and_tools(provider, None, CHECKPOINT_KIND)
    assert messages[:-1] == list(receipt.messages)
    assert messages[-1] == {"role": "user", "content": provider.instruction}
    assert tools == list(receipt.tools)


@pytest.mark.asyncio
async def test_idle_native_fold_skips_when_the_warm_input_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anchor the writer's window cannot hold skips; it never falls back cold."""
    from core.checkpoint_types import IdleCheckpointReason, IdleCheckpointSkip
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread import idle_checkpoint

    due = IdleCheckpointDue(
        "turn-1", 1, "thread-1", "2026-01-01T00:00:00",
        native_receipt=_native_receipt(provider_input_tokens=17_000),
    )
    context = _native_idle_context(due, context_window=18_000)

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(idle_checkpoint, "run_in_db", immediate)
    monkeypatch.setattr(idle_checkpoint, "read_idle_context", lambda _due: context)

    preparation = await idle_checkpoint.prepare_idle_checkpoint(due, "cp-1")
    skip = await idle_checkpoint.complete_idle_checkpoint(preparation)

    assert skip == IdleCheckpointSkip(
        IdleCheckpointReason.WARM_CAPTURE_REFUSED, cause="input_too_large",
    )


def _seed_native_idle_turn(isolated_db, *, words=500, passive_rows=0) -> None:
    """One completed native Turn, by default far above the idle gate.

    ``passive_rows`` large passive context events land after the Turn.
    """
    from infra.thread_driver import admit_input

    connection = sqlite3.connect(str(isolated_db))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,"
            "created_at,updated_at) VALUES ('thread-1','user-1',?,0,1,'t0','t0')",
            (EXTERNAL_MODEL,),
        )
        connection.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) VALUES "
            "(?,'native','external',?,?,?,200000,'t0','t0')",
            (EXTERNAL_MODEL, EXTERNAL_MODEL, EXTERNAL_MODEL, EXTERNAL_MODEL),
        )
        connection.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,turn_order,model,"
            "status,started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
            "VALUES ('turn-1','thread-1',0,1,?,'completed','t0',"
            "'2026-01-01T00:00:00','boot',1)",
            (EXTERNAL_MODEL,),
        )
        for seq in range(1, 121):
            connection.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
                "active_turn_id,delivery_state) VALUES (?,'thread-1',?,?, 't', ?,"
                "'turn-1','delivered')",
                (
                    f"m-{seq}",
                    "user" if seq == 1 else "assistant",
                    "word " * words,
                    seq,
                ),
            )
        connection.execute("UPDATE threads SET next_message_seq=121 WHERE id='thread-1'")
        for index in range(passive_rows):
            admit_input(
                "thread-1", role="system", content="word " * 2_000,
                intent="queue_after_turn", message_id=f"passive-{index}",
                turn_id="turn-2", boot_epoch="boot", admitted_at="t2",
                metadata={"type": "context_event", "event_disposition": "passive"},
                db=connection,
            )
        connection.commit()
    finally:
        connection.close()


def _install_before_fire(isolated_db) -> None:
    """A fold installed after the receipt froze and before the timer fired."""
    connection = sqlite3.connect(str(isolated_db))
    try:
        connection.execute(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) VALUES ('gen-new','thread-1',"
            "0,10,'m-10','pressure','warm','running','native',?,'native',?,0,'[]','t1')",
            (EXTERNAL_MODEL, EXTERNAL_MODEL),
        )
        connection.execute(
            "UPDATE checkpoints SET status='installed',raw_result='fold',"
            "continuity_snapshot_json='[]',captured_at='t2',installed_at='t2' "
            "WHERE id='gen-new'"
        )
        connection.commit()
    finally:
        connection.close()


async def _fire_native(isolated_db, monkeypatch, due, *, before_fire=None, **seed):
    from loguru import logger

    from core import checkpoint_runtime
    from thread import idle_checkpoint

    _seed_native_idle_turn(isolated_db, **seed)
    if before_fire is not None:
        before_fire(isolated_db)
    launched: list = []
    monkeypatch.setattr(
        checkpoint_runtime, "_launch_checkpoint",
        lambda request, provider: launched.append((request, provider)),
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "infra.product_events.emit_product_event",
        lambda _category, action, **kwargs: events.append((action, kwargs)),
    )
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        await idle_checkpoint.fire_idle_checkpoint(due)
    finally:
        logger.remove(sink)
    verify = sqlite3.connect(str(isolated_db))
    verify.row_factory = sqlite3.Row
    try:
        pending = verify.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0]
        rows = [dict(row) for row in verify.execute("SELECT * FROM checkpoints")]
    finally:
        verify.close()
    return launched, events, pending, rows, lines


@pytest.mark.asyncio
async def test_fire_folds_one_native_marker_warm_from_its_receipt(
    isolated_db, monkeypatch,
) -> None:
    """A native marker carrying its Turn's receipt consumes, rows warm, and launches.

    The fold covers through the Turn's answer row, the receipt's own end.
    """
    from core.checkpoint_types import IdleCheckpointReason
    from infra.idle_checkpoints import IdleCheckpointDue

    due = IdleCheckpointDue(
        "turn-1", 1, "thread-1", "2026-01-01T00:00:00",
        native_receipt=_native_receipt(),
    )
    launched, events, pending, rows, _lines = await _fire_native(
        isolated_db, monkeypatch, due,
    )

    assert len(launched) == 1
    request, provider = launched[0]
    assert (request.mode, request.trigger, request.writer_harness) == (
        "warm", "idle", "native",
    )
    assert request.through_message_id == "m-120"
    assert provider.warm_source is not None
    assert provider.cold_source is None
    assert provider.same_model_cold is None
    assert pending == 0
    assert [(row["mode"], row["trigger"], row["status"]) for row in rows] == [
        ("warm", "idle", "running"),
    ]
    assert events == [(
        "started",
        {
            "thread_id": "thread-1",
            "properties": {
                "reason": IdleCheckpointReason.FIRED.value,
                "through_seq": 120,
                "writer_harness": "native",
            },
        },
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_message_id", "before_fire", "cause"),
    [
        (None, None, "no_receipt"),
        ("m-119", None, "boundary_mismatch"),
        # The receipt froze on no checkpoint; one installed before the timer.
        ("m-120", _install_before_fire, "base_changed"),
    ],
    ids=["no-receipt", "boundary-mismatch", "base-changed"],
)
async def test_fire_skips_a_native_marker_it_cannot_fold_warm(
    isolated_db, monkeypatch, final_message_id, before_fire, cause,
) -> None:
    """No receipt (restart, errored final call), another end or another base
    skips with its logged cause; never cold."""
    import dataclasses

    from core.checkpoint_types import IdleCheckpointReason
    from infra.idle_checkpoints import IdleCheckpointDue

    # Built exactly as boot recovery builds a marker, with no receipt.
    due = IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00")
    if final_message_id is not None:
        due = dataclasses.replace(due, native_receipt=_native_receipt(final_message_id))
    launched, events, pending, rows, lines = await _fire_native(
        isolated_db, monkeypatch, due, before_fire=before_fire,
    )

    assert launched == []
    assert [row for row in rows if row["trigger"] == "idle"] == []
    assert pending == 0
    assert events == [(
        "refused",
        {
            "thread_id": "thread-1",
            "properties": {
                "reason": IdleCheckpointReason.WARM_CAPTURE_REFUSED.value, "cause": cause,
            },
        },
    )]
    assert any(
        "idle_checkpoint not_admitted" in line and f"cause={cause} " in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_passive_events_after_a_native_turn_never_admit_its_fold(
    isolated_db, monkeypatch,
) -> None:
    """Passive events queued after a native Turn never lift its fold over the gate.

    They stay out of the projection until their own Turn runs, so the gate's
    sealed end is the due Turn's end, the native fold's own end.
    """
    from core.checkpoint_types import IdleCheckpointReason
    from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS
    from infra.idle_checkpoints import IdleCheckpointDue

    due = IdleCheckpointDue(
        "turn-1", 1, "thread-1", "2026-01-01T00:00:00",
        native_receipt=_native_receipt(),
    )
    launched, events, pending, rows, _lines = await _fire_native(
        isolated_db, monkeypatch, due, words=5, passive_rows=40,
    )

    assert launched == []
    assert rows == []
    assert pending == 0
    [(action, event)] = events
    properties = event["properties"]
    assert (action, properties["reason"]) == (
        "refused", IdleCheckpointReason.BELOW_MINIMUM.value,
    )
    assert properties["foldable_tokens"] < IDLE_MIN_DELTA_TOKENS


def test_marker_consume_rejects_every_cold_idle_request() -> None:
    """An idle fold is warm or absent, for a native writer as for any other."""
    import dataclasses

    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1", model=EXTERNAL_MODEL)
        _turn(db, "thread-1", "turn-1", pending=1, model=EXTERNAL_MODEL)
        _message(db, "thread-1")
        for harness in ("native", "claude-code"):
            cold = dataclasses.replace(
                _request("thread-1", "turn-1"),
                mode="cold", writer_harness=harness, source_harness=harness,
            )
            with pytest.raises(ValueError, match="does not match its Turn marker"):
                consume_idle_marker("turn-1", cold, db=db)
    finally:
        db.close()


def _due():
    from infra.idle_checkpoints import IdleCheckpointDue

    return IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00")


def _patch_fire(monkeypatch, *, prepare=None, complete=None, request=None, fail=None):
    """Route one timer firing through fakes; return the emitted product events."""
    from thread import idle_checkpoint

    events: list[tuple[str, dict]] = []

    async def prepared(item, checkpoint_id):
        return SimpleNamespace(
            due=item,
            request=SimpleNamespace(id=checkpoint_id, thread_id=item.thread_id),
        )

    async def completed(preparation):
        return SimpleNamespace(request=preparation.request, provider_request=object())

    async def unexpected(*_args):
        raise AssertionError("unexpected checkpoint runtime call")

    monkeypatch.setattr(idle_checkpoint, "prepare_idle_checkpoint", prepare or prepared)
    monkeypatch.setattr(idle_checkpoint, "complete_idle_checkpoint", complete or completed)
    monkeypatch.setattr(
        "core.checkpoint_runtime.request_idle_checkpoint", request or unexpected
    )
    monkeypatch.setattr("core.checkpoint_runtime.fail_idle_checkpoint", fail or unexpected)
    monkeypatch.setattr(
        "infra.product_events.emit_product_event",
        lambda _category, action, **kwargs: events.append((action, kwargs)),
    )
    return events


@pytest.mark.asyncio
async def test_fire_starts_one_admitted_warm_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from thread import idle_checkpoint

    requested = []

    async def request(turn_id, admission):
        requested.append((turn_id, admission.request.thread_id))
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.ADMITTED,
            # A real ADMITTED result always carries FIRED; using a
            # different value here proves the emitter reads result.reason
            # instead of hardcoding FIRED.
            reason=IdleCheckpointReason.STALE,
            record=SimpleNamespace(through_seq=4, writer_harness="claude-code"),
        )

    events = _patch_fire(monkeypatch, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert requested == [("turn-1", "thread-1")]
    assert events == [(
        "started",
        {
            "thread_id": "thread-1",
            "properties": {
                "reason": "stale", "through_seq": 4, "writer_harness": "claude-code",
            },
        },
    )]


@pytest.mark.asyncio
async def test_fire_records_a_failed_marker_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from thread import idle_checkpoint

    failed = []

    async def request(_turn_id, _admission):
        raise sqlite3.OperationalError("injected consume failure")

    async def fail(turn_id, request, error):
        failed.append((turn_id, request.thread_id, type(error).__name__))
        raise sqlite3.OperationalError("injected persistent storage failure")

    events = _patch_fire(monkeypatch, request=request, fail=fail)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert failed == [("turn-1", "thread-1", "OperationalError")]
    assert [action for action, _ in events] == ["failed"]


@pytest.mark.asyncio
async def test_fire_records_unexpected_warm_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thread import idle_checkpoint

    failed = []

    async def complete(_preparation):
        raise RuntimeError("warm source exploded")

    async def fail(turn_id, request, error):
        failed.append((turn_id, request.thread_id, str(error)))

    events = _patch_fire(monkeypatch, complete=complete, fail=fail)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert failed == [("turn-1", "thread-1", "warm source exploded")]
    assert events[0][1]["properties"] == {"error": "RuntimeError"}


@pytest.mark.asyncio
async def test_fire_consumes_preparation_failure_without_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thread import idle_checkpoint

    failed = []

    async def prepare(_item, _checkpoint_id):
        raise ValueError("invalid model metadata")

    async def fail(turn_id, request, error):
        failed.append((turn_id, request, str(error)))

    events = _patch_fire(monkeypatch, prepare=prepare, fail=fail)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert failed == [("turn-1", None, "invalid model metadata")]
    assert [action for action, _ in events] == ["failed"]


@pytest.mark.asyncio
async def test_refused_warm_capture_is_an_expected_one_shot_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason, IdleCheckpointSkip
    from thread import idle_checkpoint

    requested = []

    async def warm_capture_refused(_preparation):
        return IdleCheckpointSkip(IdleCheckpointReason.WARM_CAPTURE_REFUSED)

    async def request(turn_id, admission):
        requested.append((turn_id, admission))
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.REFUSED, record=None, reason=None,
        )

    events = _patch_fire(monkeypatch, complete=warm_capture_refused, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert requested == [("turn-1", None)]
    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "warm_capture_refused"}},
    )]


@pytest.mark.asyncio
async def test_fire_below_minimum_records_measured_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason, IdleCheckpointSkip
    from thread import idle_checkpoint

    async def prepare(_item, _checkpoint_id):
        return IdleCheckpointSkip(
            IdleCheckpointReason.BELOW_MINIMUM, foldable_tokens=1_000, threshold_tokens=32_768,
        )

    async def request(turn_id, admission):
        assert admission is None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.REFUSED, record=None, reason=None,
        )

    events = _patch_fire(monkeypatch, prepare=prepare, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused",
        {
            "thread_id": "thread-1",
            "properties": {
                "reason": "below_minimum", "foldable_tokens": 1_000, "threshold_tokens": 32_768,
            },
        },
    )]


@pytest.mark.asyncio
async def test_fire_no_boundary_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason, IdleCheckpointSkip
    from thread import idle_checkpoint

    async def prepare(_item, _checkpoint_id):
        return IdleCheckpointSkip(IdleCheckpointReason.NO_BOUNDARY)

    async def request(turn_id, admission):
        assert admission is None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.REFUSED, record=None, reason=None,
        )

    events = _patch_fire(monkeypatch, prepare=prepare, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "no_boundary"}},
    )]


@pytest.mark.asyncio
async def test_fire_turn_running_precheck_records_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason, IdleCheckpointSkip
    from thread import idle_checkpoint

    async def prepare(_item, _checkpoint_id):
        return IdleCheckpointSkip(IdleCheckpointReason.TURN_RUNNING)

    async def request(turn_id, admission):
        assert admission is None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.REFUSED, record=None, reason=None,
        )

    events = _patch_fire(monkeypatch, prepare=prepare, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "turn_running"}},
    )]


@pytest.mark.asyncio
async def test_fire_stale_marker_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason, IdleCheckpointSkip
    from thread import idle_checkpoint

    async def prepare(_item, _checkpoint_id):
        return IdleCheckpointSkip(IdleCheckpointReason.STALE)

    async def request(turn_id, admission):
        assert admission is None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.STALE,
            record=None,
            reason=IdleCheckpointReason.STALE,
        )

    events = _patch_fire(monkeypatch, prepare=prepare, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "stale"}},
    )]


@pytest.mark.asyncio
async def test_fire_already_covered_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from thread import idle_checkpoint

    async def request(turn_id, admission):
        assert admission is not None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.REFUSED,
            record=None,
            reason=IdleCheckpointReason.ALREADY_COVERED,
        )

    events = _patch_fire(monkeypatch, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "already_covered"}},
    )]


@pytest.mark.asyncio
async def test_fire_coalesced_marker_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from thread import idle_checkpoint

    async def request(turn_id, admission):
        assert admission is not None
        return SimpleNamespace(
            disposition=IdleCheckpointDisposition.COALESCED,
            record=SimpleNamespace(through_seq=4, writer_harness="claude-code"),
            reason=IdleCheckpointReason.COALESCED,
        )

    events = _patch_fire(monkeypatch, request=request)

    await idle_checkpoint.fire_idle_checkpoint(_due())

    assert events == [(
        "refused", {"thread_id": "thread-1", "properties": {"reason": "coalesced"}},
    )]


def test_second_marker_consumer_is_typed_stale() -> None:
    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        first = consume_idle_marker("turn-1", None, db=db)
        second = consume_idle_marker("turn-1", None, db=db)
        assert first.disposition is IdleCheckpointDisposition.REFUSED
        assert first.reason is IdleCheckpointReason.NOT_REQUESTED
        assert second.disposition is IdleCheckpointDisposition.STALE
        assert second.reason is IdleCheckpointReason.STALE
    finally:
        db.close()


def test_admission_conflict_reports_the_underlying_conflict_kind() -> None:
    """A non-idle checkpoint already running maps to its own reason, not a guess."""
    from dataclasses import replace

    from core.checkpoint_types import IdleCheckpointDisposition, IdleCheckpointReason
    from infra.checkpoints import create_running
    from infra.idle_checkpoints import consume_idle_marker

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", pending=1)
        _message(db, "thread-1")
        create_running(
            replace(_request("thread-1", "turn-1"), id="cp-manual", trigger="manual"),
            db=db,
        )

        result = consume_idle_marker("turn-1", _request("thread-1", "turn-1"), db=db)

        assert result.disposition is IdleCheckpointDisposition.REFUSED
        assert result.reason is IdleCheckpointReason.CONCURRENT_CHECKPOINT
    finally:
        db.close()


def test_idle_threshold_is_exactly_32768_tokens() -> None:
    from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS

    assert IDLE_MIN_DELTA_TOKENS == 32_768
