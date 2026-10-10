"""Public commit contracts for the clean model-switch boundary."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Callable
from unittest.mock import AsyncMock

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/source','t0','t0')"
    )
    return db


def _switch_preparation(db, monkeypatch):
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch
    from thread import model_source
    from thread.model_projection import read_model_switch_fit_input, read_model_switch_input_proof
    from thread.model_row import _model_switch_transcript_version
    from thread.model_state import ModelSwitchPreparation, ModelSwitchProjectionProof
    from thread.transcript_order import destructive_boundary_version

    request_switch(
        "thread-1", "native/target", operation_id="switch-1",
        requested_at="2026-08-25T12:00:00", db=db,
    )
    accept_thread_input(
        "thread-1", role="user", content="after", intent="apply_now",
        message_id="target-input", db=db,
    )
    target = SimpleNamespace(
        catalog_id="native/target", harness="native", available=True,
        context_window=100_000, supports_vision=True,
    )
    source = SimpleNamespace(catalog_id="native/source", harness="native")
    monkeypatch.setattr(
        model_source, "_execution_target",
        lambda model, db=None: target if model == "native/target" else source,
    )
    fit = read_model_switch_fit_input(
        db,
        "thread-1",
        destination_model="native/target",
        destination_target=target,
    )
    return ModelSwitchPreparation(
        thread_id="thread-1", user_id="user-1", generation=1,
        source_model="native/source", source_selected_model="native/source",
        source_harness="native",
        destination_model="native/target", destination_target=target,
        project_id=None, source_runtime_context_epoch=0,
        source_runtime_owner=None, history_epoch=0, sealed_boundary=None,
        destructive_version=destructive_boundary_version(db, "thread-1"),
        transcript_version=_model_switch_transcript_version(db, "thread-1"),
        source_active_turn_id=None, projection_mode="exact",
        projection_proof=ModelSwitchProjectionProof(
            mode="exact", checkpoint_id=None, checkpoint_boundary=None,
        ),
        destination_fit_ledger=SimpleNamespace(input_identity=fit.identity),
        input_proof=read_model_switch_input_proof(db, "thread-1", fit),
    )


def _commit_racer_before_admission(monkeypatch, path, racer: Callable[[], None]) -> None:
    """Serve the commit a connection whose write admission first commits one competing writer.

    The racer lands after the unlocked validation and before the writer lock:
    FIFO admission lets a writer in this process commit only then, and at
    BEGIN IMMEDIATE it would be a refused second outermost write on this thread.
    """
    from infra import write_admission

    admit = write_admission.admit
    opened: list[sqlite3.Connection] = []
    pending = [racer]

    def connection():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    def admit_after_racer(db, scope):
        if pending and any(db is commit_db for commit_db in opened):
            pending.pop()()
        return admit(db, scope)

    monkeypatch.setattr(write_admission, "admit", admit_after_racer)
    monkeypatch.setattr("infra.database.get_db", connection)


def _file_switch(tmp_path, monkeypatch, *, add_tool: bool = False):
    path = tmp_path / "switch.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    from infra.schema import SCHEMA_SQL

    setup.executescript(SCHEMA_SQL)
    setup.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/source','t0','t0')"
    )
    if add_tool:
        from infra.queries import insert_message
        insert_message("thread-1", "tool", "raw output", {"turn_unbound_reason": "legacy_import"},
                       msg_id="tool-before-switch", db=setup)
    preparation = _switch_preparation(setup, monkeypatch)
    setup.commit()
    setup.close()
    return path, preparation


def test_prepared_commit_uses_execution_epoch_without_legacy_retarget(
    tmp_path, monkeypatch,
) -> None:
    """The guarded public commit consumes the clean pending switch row."""
    from thread.model_commit import _commit_model_switch_transaction

    path, preparation = _file_switch(tmp_path, monkeypatch)

    def connection():
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr("infra.database.get_db", connection)
    _commit_model_switch_transaction(
        preparation,
        user_id="user-1",
        now="2026-08-25T12:00:01",
        reason=None,
        actor_thread_id=None,
        actor_title=None,
    )

    verified = connection()
    try:
        session = verified.execute(
            "SELECT model,execution_epoch,pending_model,switch_status "
            "FROM threads WHERE id='thread-1'",
        ).fetchone()
        assert tuple(session) == ("native/target", 1, None, None)
        queued = verified.execute(
            "SELECT target_execution_epoch,target_model,active_turn_id "
            "FROM messages WHERE id='target-input'",
        ).fetchone()
        assert tuple(queued) == (1, "native/target", None)
    finally:
        verified.close()


def test_prepared_commit_keeps_render_and_fit_validation_before_write_lock(
    tmp_path, monkeypatch,
) -> None:
    """Only the shared transcript witness is repeated under the writer lock."""
    from thread import model_commit, model_source
    path, preparation = _file_switch(tmp_path, monkeypatch)

    def connection():
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr("infra.database.get_db", connection)
    observed: list[tuple[str, bool]] = []
    project_input = model_commit.read_model_switch_fit_input
    resolve_target = model_source._execution_target
    transcript_version = model_commit._model_switch_transcript_version

    def traced_input(db, *args, **kwargs):
        observed.append(("input", db.in_transaction))
        return project_input(db, *args, **kwargs)

    def traced_target(model, *, db=None):
        observed.append(("target", bool(db and db.in_transaction)))
        return resolve_target(model, db=db)

    def traced_version(db, thread_id):
        observed.append(("transcript", db.in_transaction))
        return transcript_version(db, thread_id)

    monkeypatch.setattr(model_commit, "read_model_switch_fit_input", traced_input)
    monkeypatch.setattr(model_source, "_execution_target", traced_target)
    monkeypatch.setattr(model_commit, "_model_switch_transcript_version", traced_version)

    model_commit._commit_model_switch_transaction(
        preparation,
        user_id="user-1",
        now="2026-08-25T12:00:01",
        reason=None,
        actor_thread_id=None,
        actor_title=None,
    )

    assert observed == [
        ("transcript", False),
        ("input", False),
        ("target", False),
        ("transcript", True),
        ("target", True),
    ]


@pytest.mark.parametrize("race", ["edited_input", "cancelled_input", "retention_stub", "replaced_switch"])
def test_prepared_commit_rejects_transcript_change_before_cas(
    tmp_path, monkeypatch, race: str,
) -> None:
    """The writer-side CAS rejects FIFO changes and in-place retention rewrites."""
    from infra.input_admission import accept_thread_input
    from infra.queued_input_settlement import cancel_queued_input
    from thread.model_commit import _commit_model_switch_transaction
    from thread.model_state import ModelSwitchSequencingRequired

    path, preparation = _file_switch(tmp_path, monkeypatch, add_tool=race == "retention_stub")

    def mutate_transcript() -> None:
        competing = sqlite3.connect(path)
        competing.row_factory = sqlite3.Row
        try:
            if race == "edited_input":
                competing.execute(
                    "UPDATE messages SET content='edited',delivery_revision=delivery_revision+1 "
                    "WHERE id='target-input'",
                )
            elif race == "replaced_switch":
                competing.execute("UPDATE threads SET switch_operation_id='replacement' WHERE id='thread-1'")
            elif race == "cancelled_input":
                cancel_queued_input("thread-1", "target-input", revision=1, db=competing)
            else:
                from core.thread_lifecycle import TOOL_TRIM_MARKER
                competing.execute(
                    "UPDATE messages SET content=? WHERE id='tool-before-switch'",
                    (f"{TOOL_TRIM_MARKER} after retention]",),
                )
            competing.commit()
        finally:
            competing.close()

    _commit_racer_before_admission(monkeypatch, path, mutate_transcript)

    with pytest.raises(ModelSwitchSequencingRequired):
        _commit_model_switch_transaction(
            preparation, user_id="user-1", now="2026-08-25T12:00:01",
            reason=None, actor_thread_id=None, actor_title=None,
        )


def test_later_queued_arrival_does_not_invalidate_the_prepared_prefix(tmp_path, monkeypatch):
    """A later unaccepted suffix must not inflate or starve this selected call."""
    from infra.input_admission import accept_thread_input
    from thread.model_commit import _commit_model_switch_transaction

    path, preparation = _file_switch(tmp_path, monkeypatch)

    def append_suffix():
        competing = sqlite3.connect(path)
        competing.row_factory = sqlite3.Row
        try:
            accept_thread_input(
                "thread-1", role="user", content="later suffix", intent="apply_now",
                message_id="later", db=competing,
            )
        finally:
            competing.close()

    _commit_racer_before_admission(monkeypatch, path, append_suffix)
    _commit_model_switch_transaction(
        preparation, user_id="user-1", now="2026-08-25T12:00:01",
        reason=None, actor_thread_id=None, actor_title=None,
    )
    with sqlite3.connect(path) as checked:
        assert checked.execute("SELECT model FROM threads WHERE id='thread-1'").fetchone()[0] == "native/target"
        assert checked.execute("SELECT content,delivery_state,active_turn_id FROM messages WHERE id='later'").fetchone() == ("later suffix", "queued", None)


def test_prepared_commit_rejects_destination_change_before_cas(tmp_path, monkeypatch) -> None:
    """Destination availability remains part of the writer-side CAS."""
    from thread import model_source
    from thread.model_commit import _commit_model_switch_transaction
    from thread.model_state import ModelSwitchConflict

    path, preparation = _file_switch(tmp_path, monkeypatch)
    setup = sqlite3.connect(path)
    setup.execute(
        "INSERT INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,available,created_at,updated_at) "
        "VALUES ('native/target','native','test','target','native/target',"
        "'Target','local','test',1,'t0','t0')"
    )
    setup.commit()
    setup.close()

    def resolve_target(model, *, db=None):
        assert model == "native/target" and db is not None
        available = bool(db.execute(
            "SELECT available FROM models WHERE id='native/target'",
        ).fetchone()[0])
        if available:
            return preparation.destination_target
        changed = vars(preparation.destination_target).copy()
        changed["available"] = False
        return SimpleNamespace(**changed)

    def change_destination() -> None:
        competing = sqlite3.connect(path)
        try:
            competing.execute(
                "UPDATE models SET available=0 WHERE id='native/target'"
            )
            competing.commit()
        finally:
            competing.close()

    monkeypatch.setattr(model_source, "_execution_target", resolve_target)
    _commit_racer_before_admission(monkeypatch, path, change_destination)

    with pytest.raises(ModelSwitchConflict, match="destination changed"):
        _commit_model_switch_transaction(
            preparation, user_id="user-1", now="2026-08-25T12:00:01",
            reason=None, actor_thread_id=None, actor_title=None,
        )


@pytest.mark.asyncio
async def test_model_switch_broadcasts_the_exact_committed_transcript_row(
    monkeypatch,
) -> None:
    """Live ordering comes from the durable row, never a hand-built payload."""
    from thread import model_runtime

    transition = AsyncMock(return_value=True)
    broadcast_rows = AsyncMock()
    monkeypatch.setattr(model_runtime, "_transition_source_runtime", transition)
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", broadcast_rows,
    )

    await model_runtime.post_model_switch_effects(
        "thread-1",
        new_model="native/target",
        user_id="user-1",
        source_harness="native",
        source_runtime_owner=None,
        breadcrumb={"id": "switch-message"},
    )

    broadcast_rows.assert_awaited_once_with("thread-1", ("switch-message",))


@pytest.mark.asyncio
async def test_public_switch_accepts_a_running_turn_without_opening_checkpoint(
    tmp_path, monkeypatch,
) -> None:
    """Busy is a durable pending switch, not a checkpoint or a rejection."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.model_switch_types import SwitchRequest
    from infra.thread_driver import admit_input
    from thread.model import switch_model

    path = tmp_path / "public-switch.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    from infra.schema import SCHEMA_SQL

    setup.executescript(SCHEMA_SQL)
    setup.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/source','t0','t0')"
    )
    setup.commit()
    admission = admit_input(
        "thread-1",
        role="user",
        content="source work",
        intent="apply_now",
        message_id="source-input",
        turn_id="source-turn",
        boot_epoch="boot-1",
        admitted_at="t1",
        db=setup,
    )
    assert admission.batch is not None
    begin_batch_write(
        admission.batch, attempt_id="source-write", started_at="t1", db=setup,
    )
    settle_batch_write("source-write", outcome="accepted", db=setup)
    setup.commit()
    setup.close()

    def connection():
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr("infra.database.get_db", connection)
    result = await switch_model(
        "thread-1",
        "native/target",
        user_id="user-1",
        actor_kind="user",
        actor_id="user-1",
        reason="harness_model_switch",
    )

    assert isinstance(result, SwitchRequest)
    assert result.status == "waiting_for_turn"
    verified = connection()
    try:
        row = verified.execute(
            "SELECT model,pending_model,switch_status,switch_after_input_order "
            "FROM threads WHERE id='thread-1'",
        ).fetchone()
        assert tuple(row) == (
            "native/source", "native/target", "preparing",
            admission.accepted.input_order,
        )
        assert verified.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id='thread-1' "
            "AND status IN ('running','captured')",
        ).fetchone() is None
    finally:
        verified.close()
