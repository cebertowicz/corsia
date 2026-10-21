"""External provider action contracts for a claimed input batch."""

from __future__ import annotations

from llm.context_window import WindowFacts
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

HUMAN_FOOTER = (
    '[To reply, call post_update(text="<your reply>")]\n'
    "If no reply is needed, do not send one."
)
from core.continuity_accounting import ContinuityAccounting

# These waits cross real SQLite commits. The bound only stops a hung test from
# eating the job; it is not a latency assertion, so a stalled shared runner
# (ten-second commits have been observed) must not fail it.
HANG_GUARD_SECONDS = 30


def _prepared_map_context(provider: str, snapshot: dict):
    """Each harness's prepared shape, carrying what its collection delivered."""
    from core.installed_context import InstalledDelivery, read_boundary
    from infra.database import get_db

    db = get_db()
    try:
        boundary_id, stored = read_boundary("thread-1", db)
    finally:
        db.close()
    delivery = InstalledDelivery(
        thread_id="thread-1", adoption="current-no-anchor",
        boundary_id=boundary_id, stored=stored, snapshot=snapshot,
    )
    if provider == "native":
        return SimpleNamespace(installed=delivery)
    carrier = SimpleNamespace(installed=delivery)
    payload = (
        SimpleNamespace(seed_reconstruction=carrier)
        if provider == "cc"
        else SimpleNamespace(reconstruction=carrier)
    )
    return SimpleNamespace(
        mode="exact_replay",
        rendered=SimpleNamespace(payload=payload),
    )


def _claimed(db: sqlite3.Connection):
    from infra.schema import SCHEMA_SQL
    from infra.thread_driver import admit_input

    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    return admission.batch


def _admitted_external_batch(*, model: str = "claude-code/sonnet"):
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1',?,'t0','t0')",
            (model,),
        )
        db.commit()
        admission = admit_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
            admitted_at="t1", db=db,
        )
    finally:
        db.close()
    assert admission.batch is not None
    return admission.batch


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("boundary", "checkpoint", "frame", "feed", "unacknowledged", "publication"))
async def test_cc_admitted_failure_keeps_one_owner_and_drives_after_unlock(
    isolated_db, monkeypatch, failure_stage: str,
) -> None:
    """An admitted failure cannot fall back to a second terminal/write owner."""
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_render import PreparedBatch
    from subagents import cc_accept, cc_feed, cc_runtime
    from tests.cc_runtime_fixtures import cc_target
    import infra.input_writes as writes
    import infra.send_dispatch as dispatch
    import infra.thread_execution as execution

    batch = _admitted_external_batch()
    accept_thread_input(
        "thread-1", role="user", content="next", intent="apply_now",
        message_id="input-2", created_at="t2",
    )
    target = cc_target("sonnet")
    prepared = PreparedBatch(
        thread_row={"id": "thread-1", "user_id": "user-1", "project_id": None},
        target=target, provider="cc", is_subagent=False, content="hello",
        agent_envelope=None, attachments=[], media_projection=None,
        prepared_context=None, refresh_prepared_context=None, runtime_truth={},
    )
    events = []
    observed_owners = []
    detail = (
        "Claude Code did not acknowledge input delivery"
        if failure_stage == "unacknowledged" else "admitted failure"
    )

    class Session:
        async def feed(self, *_args, **_kwargs):
            if failure_stage == "unacknowledged":
                return
            raise RuntimeError("admitted failure")

    live = cc_runtime._Live(Session(), None, "cc", model="sonnet")
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("thread-1", live)
    lock = cc_runtime._LIVE.lock("thread-1")

    async def prepare(*_args, **_kwargs):
        return prepared

    async def boundary(current, identity):
        assert identity == batch.identity
        if failure_stage == "checkpoint":
            from core.checkpoint_types import CheckpointUnavailable

            raise CheckpointUnavailable(
                "admitted failure", context_window=200_000, installed_checkpoint_id=None,
            )
        if failure_stage == "boundary":
            raise RuntimeError("admitted failure")
        return current, SimpleNamespace(turn_text="hello")

    def prepare_frame(*_args):
        if failure_stage == "frame":
            raise RuntimeError("admitted failure")

    def bcast(_thread_id, payload):
        if failure_stage == "publication" and payload["type"] == "turn_complete":
            raise RuntimeError("publication failure")

    async def accept(_transition, acceptance):
        return await cc_accept.accept_reconstructed_input(
            "thread-1", live, SimpleNamespace(turn_text="hello"),
            turn_id=batch.turn_id, bcast=bcast,
            preadmitted_turn=batch.identity, before_provider_write=boundary,
            mark_accepted=acceptance.mark_accepted,
            mark_ambiguous=acceptance.mark_ambiguous,
            settle_delivery=acceptance.settle_delivery,
            transfer_delivery_to_turn=acceptance.transfer_delivery_to_turn,
            settle_delivery_terminal=acceptance.settle_delivery_terminal,
        )

    async def send(_provider, ctx):
        evidence = ctx.transfer_delivery_to_turn.__self__
        try:
            identity = await cc_feed.ensure_live_then_feed(
                "thread-1", ctx.content, target=target, turn_id=batch.turn_id,
                preadmitted_turn=batch.identity,
                mark_accepted=ctx.mark_accepted, mark_ambiguous=ctx.mark_ambiguous,
                settle_delivery=ctx.settle_delivery,
                transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
                settle_delivery_terminal=ctx.settle_delivery_terminal,
            )
            return "cc_runtime.feed_main", None, identity
        finally:
            observed_owners.append(evidence.turn_owned)

    async def reject_outer(*_args, **_kwargs):
        raise AssertionError("admitted input returned to outer write owner")

    async def drive(settlement):
        assert not lock.locked()
        assert live.turn_active is False
        # A checkpoint refusal now fails its own input visibly, like every other
        # admitted failure, so the rest of the FIFO still reaches a successor.
        assert settlement.successor is not None
        assert tuple(item.message_id for item in settlement.successor.items) == ("input-2",)
        events.append(settlement.result)

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(execution, "drive_terminal_settlement", drive)
    monkeypatch.setattr(dispatch, "dispatch", send)
    monkeypatch.setattr(writes, "_settle_evidence", reject_outer)
    monkeypatch.setattr(cc_feed, "_accept_under_lock", accept)
    monkeypatch.setattr(cc_accept, "_prepare_live_turn", prepare_frame)
    try:
        raised_detail = "publication failure" if failure_stage == "publication" else detail
        with pytest.raises(RuntimeError, match=raised_detail):
            await asyncio.wait_for(execution.execute_claimed_batch(
                batch, thread_row=prepared.thread_row, target=target,
            ), timeout=HANG_GUARD_SECONDS)
        assert observed_owners == [True]
        assert len(events) == 1
        db = get_db()
        try:
            assert tuple(db.execute(
                "SELECT status,error FROM active_turns WHERE id='turn-1'"
            ).fetchone()) == ("error", detail)
            assert tuple(db.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
            ).fetchone()) == ("failed", "proved_no_write")
        finally:
            db.close()
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("activity", "configuration", "boundary", "checkpoint", "replacement_setup", "write", "refused", "unacknowledged", "publication"))
async def test_codex_admitted_failure_keeps_one_owner_and_drives_after_unlock(
    isolated_db, monkeypatch, failure_stage: str,
) -> None:
    """Codex owns failure from admission through exact retirement and unlock."""
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_render import PreparedBatch
    from subagents import codex_runtime as runtime
    from tests.codex_runtime_fixtures import _codex_target
    import infra.input_writes as writes
    import infra.send_dispatch as dispatch
    import infra.thread_execution as execution

    batch = _admitted_external_batch(model="openai/gpt-5.6-luna")
    accept_thread_input(
        "thread-1", role="user", content="next", intent="apply_now",
        message_id="input-2", created_at="t2",
    )
    target = _codex_target("gpt-5.6-luna")
    prepared = PreparedBatch(
        thread_row={"id": "thread-1", "user_id": "user-1", "project_id": None},
        target=target, provider="codex", is_subagent=False, content="hello",
        agent_envelope=None, attachments=[], media_projection=None,
        prepared_context=None, refresh_prepared_context=None, runtime_truth={},
    )
    evidence_refs, driven = [], []
    runtime._LIVE.reset()
    lock = runtime._LIVE.lock("thread-1")

    class Session:
        returncode = None
        _closing = False

        async def terminate(self):
            self._closing = True
            self.returncode = 0

    session = Session()

    async def prepare(*_args, **_kwargs):
        return prepared

    async def prepare_context(*_args, **_kwargs):
        return None, False

    def bcast(_thread_id, payload):
        if failure_stage == "activity" and payload["type"] == "turn_activity":
            raise RuntimeError("admitted failure")
        if failure_stage == "publication" and payload["type"] == "turn_complete":
            raise RuntimeError("publication failure")

    async def configure(*_args, **_kwargs):
        if failure_stage == "configuration":
            raise RuntimeError("admitted failure")

        async def start_input(*_callbacks):
            if failure_stage == "unacknowledged":
                _callbacks[0]()
                return False
            if failure_stage == "refused":
                return False
            raise RuntimeError("admitted failure")

        return session, start_input

    async def boundary(spec, live):
        if failure_stage == "checkpoint":
            from core.checkpoint_types import CheckpointUnavailable

            raise CheckpointUnavailable(
                "admitted failure", context_window=100_000, installed_checkpoint_id=None,
            )
        if failure_stage == "boundary":
            raise RuntimeError("admitted failure")
        if failure_stage == "replacement_setup":
            from subagents.codex_turn_transitions import resume_same_turn_after_checkpoint_locked

            return await resume_same_turn_after_checkpoint_locked(
                spec.thread_id, live, SimpleNamespace(runtime_context_epoch=1),
                current_user_text=spec.text,
                mark_accepted=spec.mark_accepted, mark_ambiguous=spec.mark_ambiguous,
                settle_delivery=spec.settle_delivery,
                settle_delivery_terminal=spec.settle_delivery_terminal,
                held_lock=spec.held_lock,
            )
        return None

    tool_resolutions = 0

    def resolve_tools(*_args, **_kwargs):
        nonlocal tool_resolutions
        tool_resolutions += 1
        if failure_stage == "replacement_setup" and tool_resolutions == 2:
            raise RuntimeError("admitted failure")
        return object(), ()

    async def spawn_locked(thread_id, text, **kwargs):
        return await runtime._spawn_turn(
            thread_id, text, successor_turn_id=batch.turn_id, binary="codex",
            model=target.transport_model, catalog_model_id=target.catalog_id,
            windows=WindowFacts(128_000, None), broadcast=bcast, preadmitted_turn=batch.identity,
            mark_accepted=kwargs["mark_accepted"], mark_ambiguous=kwargs["mark_ambiguous"],
            settle_delivery=kwargs["settle_delivery"],
            transfer_delivery_to_turn=kwargs["transfer_delivery_to_turn"],
            settle_delivery_terminal=kwargs["settle_delivery_terminal"],
            held_lock=kwargs["held_lock"],
        )

    async def send(_provider, ctx):
        evidence_refs.append(ctx.transfer_delivery_to_turn.__self__)
        identity = await runtime.ensure_live_then_feed(
            "thread-1", ctx.content, allow_fresh=True, target=target,
            successor_turn_id=batch.turn_id, bind_input_turn=ctx.bind_input_turn,
            mark_accepted=ctx.mark_accepted, mark_ambiguous=ctx.mark_ambiguous,
            settle_delivery=ctx.settle_delivery,
            transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
            settle_delivery_terminal=ctx.settle_delivery_terminal,
            preadmitted_turn=batch.identity,
        )
        return "codex_runtime.feed_main", None, identity

    async def reject_outer(*_args, **_kwargs):
        raise AssertionError("admitted input returned to outer write owner")

    async def drive(settlement):
        assert not lock.locked()
        if failure_stage == "unacknowledged":
            assert runtime._LIVE.get("thread-1").input_delivery_unproven
        else:
            assert runtime._LIVE.get("thread-1") is None
        # A checkpoint refusal now fails its own input visibly, like every other
        # admitted failure, so the rest of the FIFO still reaches a successor.
        assert settlement.successor is not None
        assert tuple(item.message_id for item in settlement.successor.items) == ("input-2",)
        driven.append(settlement.result)

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(execution, "drive_terminal_settlement", drive)
    monkeypatch.setattr(dispatch, "dispatch", send)
    monkeypatch.setattr(writes, "_settle_evidence", reject_outer)
    monkeypatch.setattr(runtime, "_prepare_dispatch_context", prepare_context)
    monkeypatch.setattr(runtime, "_dispatch_prepared_codex", spawn_locked)
    monkeypatch.setattr(runtime, "_reusable_app_session", lambda _id: session)
    monkeypatch.setattr(runtime, "_resolve_app_server_tools", resolve_tools)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda _model, **_kwargs: target)
    monkeypatch.setattr(runtime, "_configure_spec_app_server_input", configure)
    monkeypatch.setattr(runtime, "_maintain_codex_initial_boundary", boundary)
    monkeypatch.setattr(runtime, "_resolve_spawn_payload", lambda **kw: SimpleNamespace(
        provider_text=kw["text"], history_items=(), tool_registry=object(), dynamic_tools=(),
        continuity_accounting=ContinuityAccounting.not_delivered(),
        reconstruction=SimpleNamespace(snapshot=SimpleNamespace(to_dict=lambda: {})),
    ))
    try:
        detail = "Codex did not confirm a provider turn" if failure_stage in {"refused", "unacknowledged"} else "admitted failure"
        raised_detail = "publication failure" if failure_stage == "publication" else detail
        with pytest.raises(RuntimeError, match=raised_detail):
            await asyncio.wait_for(execution.execute_claimed_batch(
                batch, thread_row=prepared.thread_row, target=target,
            ), timeout=HANG_GUARD_SECONDS)
        assert len(driven) == 1
        assert [evidence.turn_owned for evidence in evidence_refs] == [True]
        db = get_db()
        try:
            assert tuple(db.execute(
                "SELECT status,error FROM active_turns WHERE id='turn-1'"
            ).fetchone()) == ("error", (
                "Codex input delivery could not be verified; provider output was discarded."
                if failure_stage == "unacknowledged" else detail
            ))
            assert tuple(db.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
            ).fetchone()) == (
                ("ambiguous", "ambiguous") if failure_stage == "unacknowledged"
                else ("failed", "proved_no_write")
            )
        finally:
            db.close()
    finally:
        runtime._LIVE.reset()
        runtime._APP_SESSIONS.pop("thread-1", None)
        runtime._CODEX_THREADS.pop("thread-1", None)




@pytest.mark.asyncio
async def test_provider_action_settles_only_reported_write_evidence() -> None:
    """Success, ambiguity, and pre-write failure never infer one another."""
    from infra.thread_execution import dispatch_fenced_batch

    accepted_db = sqlite3.connect(":memory:")
    ambiguous_db = sqlite3.connect(":memory:")
    no_write_db = sqlite3.connect(":memory:")
    try:
        accepted = _claimed(accepted_db)

        async def _accept(evidence):
            evidence.mark_accepted()
            return "ok"

        assert await dispatch_fenced_batch(
            accepted, _accept, attempt_id="accepted", started_at="t2",
            db=accepted_db,
        ) == "ok"
        assert tuple(accepted_db.execute(
            "SELECT delivery_state,write_outcome FROM messages"
        ).fetchone()) == ("delivered", "accepted")

        ambiguous = _claimed(ambiguous_db)

        async def _ambiguous(evidence):
            evidence.mark_ambiguous()
            raise RuntimeError("connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            await dispatch_fenced_batch(
                ambiguous, _ambiguous, attempt_id="ambiguous", started_at="t2",
                db=ambiguous_db,
            )
        assert tuple(ambiguous_db.execute(
            "SELECT delivery_state,write_outcome FROM messages"
        ).fetchone()) == ("ambiguous", "ambiguous")

        no_write = _claimed(no_write_db)

        async def _fail_before_write(_evidence):
            raise ValueError("local preparation failed")

        with pytest.raises(ValueError, match="local preparation failed"):
            await dispatch_fenced_batch(
                no_write, _fail_before_write, attempt_id="no-write",
                started_at="t2", db=no_write_db,
            )
        assert tuple(no_write_db.execute(
            "SELECT delivery_state,write_outcome FROM messages"
        ).fetchone()) == ("failed", "proved_no_write")
    finally:
        accepted_db.close()
        ambiguous_db.close()
        no_write_db.close()


@pytest.mark.asyncio
async def test_provider_success_without_write_evidence_is_not_inferred() -> None:
    """A returned adapter call is not proof that the provider got the bytes."""
    from infra.thread_execution import dispatch_fenced_batch

    db = sqlite3.connect(":memory:")
    try:
        batch = _claimed(db)

        async def _returned_without_proof(_evidence):
            return "not proof"

        with pytest.raises(RuntimeError, match="no provider-write evidence"):
            await dispatch_fenced_batch(
                batch,
                _returned_without_proof,
                attempt_id="missing-proof",
                started_at="t2",
                db=db,
            )
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages"
        ).fetchone()) == ("failed", "proved_no_write")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cc_prewrite_failure_settles_fence_before_turn_terminal(
    isolated_db, monkeypatch,
) -> None:
    """The CC adapter preserves its causal error and leaves no unresolved write."""
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    import connections.models as connection_models
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution
    from tests.cc_runtime_fixtures import cc_target

    batch = _admitted_external_batch()
    target = cc_target("sonnet")
    prepared = PreparedBatch(
        thread_row={"id": "thread-1", "user_id": "user-1", "project_id": None},
        target=target,
        provider="cc",
        is_subagent=False,
        content="hello",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=object(),
        refresh_prepared_context=None,
        runtime_truth={},
    )

    async def prepare(*_args, **_kwargs):
        return prepared

    async def fail_before_write(*_args, settle_delivery_terminal, **_kwargs):
        await settle_delivery_terminal("error", "original CC startup failure")
        check = get_db()
        try:
            assert tuple(check.execute(
                "SELECT delivery_state,write_outcome FROM messages "
                "WHERE id='input-1'"
            ).fetchone()) == ("failed", "proved_no_write")
            assert tuple(check.execute(
                "SELECT status,error FROM active_turns WHERE id='turn-1'"
            ).fetchone()) == ("running", None)
        finally:
            check.close()
        raise RuntimeError("original CC startup failure")

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(send_dispatch, "dispatch_cc_main", fail_before_write)
    monkeypatch.setattr(
        connection_models, "connection_available", lambda _provider: True
    )

    with pytest.raises(RuntimeError, match="original CC startup failure"):
        await execution.execute_claimed_batch(
            batch,
            thread_row=prepared.thread_row,
            target=target,
        )

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("error", "original CC startup failure")
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()) == ("failed", "proved_no_write")
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE active_turn_id='turn-1' "
            "AND write_started_at IS NOT NULL AND write_outcome IS NULL"
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cc_ordinary_cancel_keeps_outer_write_settlement_owner(
    isolated_db, monkeypatch,
) -> None:
    """A non-Stop CC cancellation cannot strand its fenced Turn on DB failure."""
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_render import PreparedBatch
    import connections.models as connection_models
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution
    from tests.cc_runtime_fixtures import cc_target

    batch = _admitted_external_batch()
    db = get_db()
    try:
        queued = accept_thread_input(
            "thread-1",
            role="user",
            content="queued successor",
            intent="apply_now",
            message_id="input-2",
            created_at="t1.5",
            db=db,
        )
        assert queued.message_id == "input-2"
    finally:
        db.close()
    target = cc_target("sonnet")
    prepared = PreparedBatch(
        thread_row={"id": "thread-1", "user_id": "user-1", "project_id": None},
        target=target,
        provider="cc",
        is_subagent=False,
        content="hello",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=object(),
        refresh_prepared_context=None,
        runtime_truth={},
    )
    original_run_in_db = execution.run_in_db
    failed_settlement = False
    driven_settlements = []

    async def prepare(*_args, **_kwargs):
        return prepared

    async def fail_settlement_once(action, *args, **kwargs):
        nonlocal failed_settlement
        if action is execution.settle_batch_write and not failed_settlement:
            failed_settlement = True
            raise RuntimeError("input settlement persistence failed")
        return await original_run_in_db(action, *args, **kwargs)

    async def cancel_before_write(
        *_args,
        settle_delivery_terminal,
        transfer_delivery_to_turn,
        **_kwargs,
    ):
        assert transfer_delivery_to_turn is not None
        await settle_delivery_terminal("cancelled", "ordinary CC feed cancelled")
        raise AssertionError("failed settlement must remain visible")

    async def drive_terminal_settlement(settlement) -> None:
        driven_settlements.append(settlement)

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(execution, "run_in_db", fail_settlement_once)
    monkeypatch.setattr(
        execution, "drive_terminal_settlement", drive_terminal_settlement,
    )
    monkeypatch.setattr(send_dispatch, "dispatch_cc_main", cancel_before_write)
    monkeypatch.setattr(
        connection_models, "connection_available", lambda _provider: True
    )

    with pytest.raises(RuntimeError, match="input settlement persistence failed"):
        await execution.execute_claimed_batch(
            batch,
            thread_row=prepared.thread_row,
            target=target,
        )

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("error", "input settlement persistence failed")
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()) == ("failed", "proved_no_write")
    finally:
        db.close()
    assert len(driven_settlements) == 1
    assert driven_settlements[0].successor is not None
    assert tuple(
        item.message_id for item in driven_settlements[0].successor.items
    ) == ("input-2",)


@pytest.mark.parametrize("settlement_failure", ("none", "publication", "persistence"))
@pytest.mark.asyncio
async def test_codex_checkpoint_stop_transfers_no_write_before_stop_terminal(
    isolated_db,
    monkeypatch,
    settlement_failure: str,
) -> None:
    """Stop closes a pre-write Codex Turn only after its input fence settles."""
    from core.checkpoint_wait import await_checkpoint_or_stop
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_render import PreparedBatch
    from subagents import codex_runtime, codex_turn_transitions
    import infra.input_writes as input_writes
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution

    thread_id = "thread-1"
    batch = _admitted_external_batch(model="openai/gpt-5.6-luna")
    turn_id = batch.turn_id
    db = get_db()
    try:
        thread_row = dict(db.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone())
        accept_thread_input(
            thread_id,
            role="user",
            content="queued successor",
            intent="apply_now",
            message_id="input-2",
            created_at="t1.5",
            db=db,
        )
        accept_thread_input(
            thread_id,
            role="user",
            content="queued successor tail",
            intent="apply_now",
            message_id="input-3",
            created_at="t1.6",
            db=db,
        )
    finally:
        db.close()

    target = SimpleNamespace(harness="codex")
    prepared = PreparedBatch(
        thread_row=thread_row,
        target=target,
        provider="codex",
        is_subagent=False,
        content="hello",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=None,
        refresh_prepared_context=None,
        runtime_truth={},
    )
    checkpoint_waiting = asyncio.Event()
    foreground_waiting = asyncio.Event()
    release_foreground = asyncio.Event()
    live_ref: list[codex_runtime._LiveCodex] = []
    evidence_ref = []
    driven_settlements = []
    settled_input = (
        ("queued", None)
        if settlement_failure == "persistence"
        else ("cancelled", "proved_no_write")
    )

    def durable_state() -> tuple[str, tuple[str, str | None]]:
        check = get_db()
        try:
            status = check.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()["status"]
            input_row = check.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
            ).fetchone()
            return status, tuple(input_row)
        finally:
            check.close()

    class Session:
        def __init__(self) -> None:
            self._closing = False
            self.returncode = None

        def _request_terminate(self) -> None:
            live = live_ref[0]
            assert live.provider_context_untrusted is True
            assert live.checkpoint_wait_scope.stop_requested is True
            self._closing = True

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            assert durable_state() == ("running", settled_input)

    session = Session()
    codex_runtime._LIVE.reset()
    transition_lock = codex_runtime._LIVE.lock(thread_id)

    async def configure(*_args, **_kwargs):
        async def start_input(*_callbacks):
            raise AssertionError("stopped input reached the provider")

        return session, start_input

    async def initial_boundary(_spec, live):
        live_ref.append(live)
        checkpoint_waiting.set()
        await await_checkpoint_or_stop(
            live.checkpoint_wait_scope,
            "checkpoint-codex-initial-stop",
            lambda _checkpoint_id: asyncio.Future(),
        )
        raise AssertionError("Stop did not release the checkpoint waiter")

    async def dispatch_codex(provider: str, ctx):
        assert provider == "codex"
        evidence = ctx.transfer_delivery_to_turn.__self__
        assert isinstance(evidence, input_writes.ProviderWriteEvidence)
        evidence_ref.append(evidence)
        async with transition_lock:
            identity = await codex_runtime._spawn_turn(
                thread_id,
                ctx.content,
                successor_turn_id=ctx.turn_id,
                current_input_message_id=ctx.input_message_id,
                codex_thread_id=None,
                binary="codex",
                working_dir=None,
                broadcast=lambda *_args: None,
                windows=WindowFacts(128_000, None),
                mark_accepted=ctx.mark_accepted,
                mark_ambiguous=ctx.mark_ambiguous,
                settle_delivery=ctx.settle_delivery,
                transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
                settle_delivery_terminal=ctx.settle_delivery_terminal,
                preadmitted_turn=ctx.preadmitted_turn,
                held_lock=transition_lock,
            )
        return "codex_runtime.feed_main", None, identity

    async def terminate_foreground(_thread_id, live) -> bool:
        assert live is live_ref[0]
        assert live.checkpoint_wait_scope.stop_requested is True
        foreground_waiting.set()
        await release_foreground.wait()
        return True

    async def reject_outer_settlement(*_args, **_kwargs):
        raise AssertionError("Codex Stop returned delivery to the outer fence")

    original_broadcast_batch = execution._broadcast_batch
    publication_injected = False

    async def maybe_fail_terminal_publication(current_batch) -> None:
        nonlocal publication_injected
        if settlement_failure == "publication" and durable_state()[1] == settled_input:
            assert durable_state() == ("running", settled_input)
            publication_injected = True
            raise RuntimeError("terminal input publication failed")
        await original_broadcast_batch(current_batch)

    original_run_in_db = execution.run_in_db

    async def maybe_fail_input_settlement(action, *args, **kwargs):
        if (
            settlement_failure == "persistence"
            and action is execution.settle_batch_write
        ):
            raise RuntimeError("input settlement persistence failed")
        return await original_run_in_db(action, *args, **kwargs)

    async def prepare(*_args, **_kwargs):
        return prepared

    async def drive_terminal_settlement(settlement) -> None:
        assert codex_runtime._LIVE.get(thread_id) is None
        driven_settlements.append((transition_lock.locked(), settlement))

    async def reject_failed_spawn(*_args, **_kwargs) -> None:
        raise AssertionError("generic spawn cleanup replaced the exact Stop owner")

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(execution, "_broadcast_batch", maybe_fail_terminal_publication)
    monkeypatch.setattr(execution, "run_in_db", maybe_fail_input_settlement)
    monkeypatch.setattr(input_writes, "_settle_evidence", reject_outer_settlement)
    monkeypatch.setattr(send_dispatch, "dispatch", dispatch_codex)
    monkeypatch.setattr(codex_runtime, "_reusable_app_session", lambda _id: session)
    monkeypatch.setattr(
        codex_runtime,
        "_resolve_spawn_payload",
        lambda **kwargs: SimpleNamespace(
            provider_text=kwargs["text"],
            reconstruction=SimpleNamespace(
                snapshot=SimpleNamespace(to_dict=lambda: {}),
            ),
            history_items=(),
            tool_registry=object(),
            dynamic_tools=(),
            continuity_accounting=ContinuityAccounting.not_delivered(),
        ),
    )
    monkeypatch.setattr(
        codex_runtime, "_resolve_app_server_tools", lambda *_a, **_k: (object(), ()),
    )
    monkeypatch.setattr(codex_runtime, "_configure_spec_app_server_input", configure)
    monkeypatch.setattr(
        codex_runtime, "_maintain_codex_initial_boundary", initial_boundary,
    )
    monkeypatch.setattr(
        codex_runtime, "_finish_failed_app_server_spawn", reject_failed_spawn,
    )
    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    monkeypatch.setattr(
        codex_turn_transitions, "drive_turn_settlement", drive_terminal_settlement,
    )

    dispatch_task = asyncio.create_task(execution.execute_claimed_batch(
        batch,
        thread_row=thread_row,
        target=target,
    ))
    stop_task = None
    try:
        await asyncio.wait_for(checkpoint_waiting.wait(), timeout=HANG_GUARD_SECONDS)
        live = live_ref[0]
        stop_task = asyncio.create_task(codex_runtime.stop(
            thread_id,
            expected_turn_id=turn_id,
            expected_live=live,
            broadcast=lambda *_args: None,
        ))
        await asyncio.wait_for(foreground_waiting.wait(), timeout=HANG_GUARD_SECONDS)
        expected_dispatch_error = {
            "none": None,
            "publication": "terminal input publication failed",
            "persistence": "input settlement persistence failed",
        }[settlement_failure]
        if expected_dispatch_error is None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(dispatch_task), timeout=HANG_GUARD_SECONDS)
        else:
            with pytest.raises(RuntimeError, match=expected_dispatch_error):
                await asyncio.wait_for(asyncio.shield(dispatch_task), timeout=HANG_GUARD_SECONDS)
        evidence = evidence_ref[0]
        assert (
            evidence.turn_owned,
            evidence.proved_no_write,
            evidence.accepted,
            evidence.ambiguous,
        ) == (True, True, False, False)
        assert codex_runtime._LIVE.get(thread_id) is live
        assert driven_settlements == []
        assert durable_state() == ("running", settled_input)
        check = get_db()
        try:
            assert [tuple(row) for row in check.execute(
                "SELECT id,delivery_state,active_turn_id FROM messages "
                "WHERE id IN ('input-2','input-3') ORDER BY input_order"
            ).fetchall()] == [
                ("input-2", "queued", None),
                ("input-3", "queued", None),
            ]
        finally:
            check.close()
        assert publication_injected is (settlement_failure == "publication")
        release_foreground.set()

        if settlement_failure == "persistence":
            with pytest.raises(RuntimeError, match="unresolved provider write"):
                await asyncio.wait_for(stop_task, timeout=HANG_GUARD_SECONDS)
            assert codex_runtime._LIVE.get(thread_id) is live
        else:
            assert await asyncio.wait_for(stop_task, timeout=HANG_GUARD_SECONDS) is True
            assert len(driven_settlements) == 1
            lock_held, settlement = driven_settlements[0]
            assert lock_held is False
            assert settlement.successor is not None
            assert tuple(
                item.message_id for item in settlement.successor.items
            ) == ("input-2", "input-3")

        expected_status = (
            "running" if settlement_failure == "persistence" else "cancelled"
        )
        assert durable_state() == (expected_status, settled_input)
    finally:
        release_foreground.set()
        tasks = [dispatch_task, *([stop_task] if stop_task is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.parametrize("is_subagent", (False, True))
@pytest.mark.asyncio
async def test_codex_dispatch_carries_terminal_write_owner_to_every_spawn(
    monkeypatch,
    is_subagent: bool,
) -> None:
    """Main and delegated Codex spawns retain their exact write-fence owner."""
    from unittest.mock import AsyncMock

    import infra.send_dispatch as send_dispatch
    from core.turn_core import TurnIdentity
    from subagents import codex_dispatch, codex_runtime
    from tests.codex_runtime_fixtures import _codex_target

    thread_id = f"codex-write-owner-{'sub' if is_subagent else 'main'}"
    identity = TurnIdentity(f"turn-{thread_id}", 1)
    prepared = object()

    def transfer_delivery() -> None:
        return None

    settle_delivery = AsyncMock()
    bind_input_turn = AsyncMock()
    has_thread = AsyncMock(return_value=is_subagent)
    prepare_context = AsyncMock(return_value=(prepared, False))
    spawn = AsyncMock(return_value=identity)

    monkeypatch.setattr(
        codex_dispatch, "_require_prepared_provider_fit", lambda *_a: None,
    )
    monkeypatch.setattr(codex_runtime, "ensure_connection_available", lambda: None)
    monkeypatch.setattr(codex_runtime, "has_thread_id", has_thread)
    monkeypatch.setattr(codex_runtime, "_prepare_dispatch_context", prepare_context)
    monkeypatch.setattr(codex_runtime, "_spawn_turn", spawn)

    codex_runtime._LIVE.reset()
    codex_runtime._DEAD.discard(thread_id)
    if is_subagent:
        codex_runtime._CODEX_THREADS[thread_id] = "provider-thread"
    try:
        ctx = send_dispatch.DispatchContext(
            thread_id=thread_id,
            thread_row={"id": thread_id, "user_id": "user-1", "project_id": None},
            content="provider input",
            is_subagent=is_subagent,
            target=_codex_target("gpt-5.6-luna"),
            turn_id=identity.turn_id,
            metadata={"role": "worker"} if is_subagent else None,
            allow_fresh=not is_subagent,
            input_message_id=f"input-{thread_id}",
            bind_input_turn=bind_input_turn,
            prepared_context=prepared,
            preadmitted_turn=identity,
            transfer_delivery_to_turn=transfer_delivery,
            settle_delivery_terminal=settle_delivery,
        )

        assert await send_dispatch.dispatch("codex", ctx) == (
            "codex_runtime.feed" if is_subagent else "codex_runtime.feed_main",
            None,
            identity,
        )
        spawn.assert_awaited_once()
        await_args = spawn.await_args
        assert await_args is not None
        assert (
            await_args.kwargs["transfer_delivery_to_turn"]
            is transfer_delivery
        )
        assert await_args.kwargs["settle_delivery_terminal"] is settle_delivery
    finally:
        codex_runtime._LIVE.reset()
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_claimed_batch_uses_one_preflight_and_exact_provider_fence(
    isolated_db, monkeypatch,
) -> None:
    """The claimed FIFO batch is the payload and write unit handed to a harness."""
    from infra.database import get_db
    from infra.thread_driver import admit_input
    import core.thread_warm as thread_warm
    from llm.resolution import resolve_execution_target
    import infra.runtime_truth as runtime_truth
    import infra.send_broadcast as send_broadcast
    import infra.send_dispatch as send_dispatch
    import infra.send_preflight as send_preflight
    from infra.thread_execution import execute_claimed_batch

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','anthropic/claude-3-5-sonnet','t0','t0')"
    )
    db.commit()
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    admission = admit_input(
        "thread-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    db.close()
    assert admission.batch is not None
    target = resolve_execution_target("anthropic/claude-3-5-sonnet")

    preflights = []
    dispatches = []
    broadcasts = []
    warm_release = asyncio.Event()
    warm_join_started = asyncio.Event()
    dispatch_started = asyncio.Event()

    async def _warm():
        await warm_release.wait()
        return {"ok": True}

    warm_task, started, suppression = await thread_warm.start_thread_warm(
        "thread-1", "existing-native-warm", _warm, model=target.catalog_id, admission_check=lambda: None,
    )
    assert started is True
    assert suppression is None

    async def _truth(*_args, **_kwargs):
        return {"addressable": True, "execution_class": "native"}

    async def _preflight(**kwargs):
        from dataclasses import replace
        from core.destination_preflight import attach_native_provider_fit_ledger
        from core.turn_context import NativePreparedContext

        assert warm_join_started.is_set()
        preflights.append(kwargs)
        messages = [{"role": "user", "content": kwargs["current_user_content"]}]
        prepared = attach_native_provider_fit_ledger(
            target_catalog_id=target.catalog_id, proven_target_window_tokens=200000,
            messages=messages, tools=[], input_identity=admission.batch.turn_id,
            current_input=messages[-1],
        )
        payload = NativePreparedContext(tuple(messages), {}, (), None)
        return {"status": "allowed", "prepared_context": replace(
            prepared, rendered=replace(prepared.rendered, payload=payload),
        )}

    original_join = thread_warm.join_requested_warm_for_delivery

    async def _join(thread_id, catalog_id):
        warm_join_started.set()
        return await original_join(thread_id, catalog_id)

    async def _dispatch(provider, ctx):
        dispatch_started.set()
        dispatches.append((provider, ctx))
        ctx.mark_accepted()
        await ctx.settle_delivery()
        return "native.fake", None, admission.batch.identity

    async def _broadcast(thread_id, message_ids):
        broadcasts.append((thread_id, message_ids))

    monkeypatch.setattr(runtime_truth, "read_runtime_truth", _truth)
    monkeypatch.setattr(thread_warm, "join_requested_warm_for_delivery", _join)
    monkeypatch.setattr(send_preflight, "context_preflight", _preflight)
    monkeypatch.setattr(send_dispatch, "dispatch", _dispatch)
    monkeypatch.setattr(send_broadcast, "broadcast_message_rows", _broadcast)

    send_task = asyncio.create_task(execute_claimed_batch(
        admission.batch, thread_row=thread_row, target=target, user={"id": "user-1"},
    ))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert dispatch_started.is_set() is False
    assert preflights == []
    warm_release.set()
    result = await send_task
    assert warm_task is not None
    assert await warm_task == {"ok": True}

    assert result.driver == "native.fake"
    assert result.turn_id == "turn-1"
    assert result.seq == admission.accepted.input_order
    assert [item.message_id for item in admission.batch.items][1:] == ["input-1"]
    assert "[PASSIVE CONTEXT DIGEST]" in admission.batch.items[0].content
    assert len(preflights) == 1
    provider_input = preflights[0]["current_user_content"]
    assert provider_input.startswith(
        "CORSIA is delivering 2 queued inbound inputs together."
    )
    assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in provider_input
    assert provider_input.endswith(f"[2/2]\nhello\n{HUMAN_FOOTER}")
    assert dispatches[0][0] == "native"
    assert len(broadcasts) == 2
    for thread_id, message_ids in broadcasts:
        assert thread_id == "thread-1"
        assert len(message_ids) == 2
        assert message_ids[0].startswith("context-digest:thread-1:")
        assert message_ids[1] == "input-1"
    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()) == ("delivered", "accepted")
    finally:
        db.close()


@pytest.mark.parametrize("provider", ["native", "cc", "codex"])
@pytest.mark.asyncio
async def test_cold_provider_dispatch_freezes_map_before_provider_io(
    isolated_db, monkeypatch, provider,
) -> None:
    """The provider sees only bytes whose responsibility map is already durable."""
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution

    batch = _admitted_external_batch()
    snapshot = {
        "model": "claude-code/sonnet",
        "execution_epoch": 0,
        "project_id": None,
        "content": "[Agent responsibility map]\nSelf: frozen",
        "agent_count": 1,
        "agent_thread_ids": ["thread-1"],
    }
    prepared = PreparedBatch(
        thread_row={"id": "thread-1", "user_id": "user-1", "project_id": None},
        target=SimpleNamespace(
            catalog_id="claude-code/sonnet", harness=provider,
        ),
        provider=provider,
        is_subagent=False,
        content="hello",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=_prepared_map_context(provider, snapshot),
        refresh_prepared_context=None,
        runtime_truth={},
    )

    async def prepare(*_args, **_kwargs):
        return prepared

    async def dispatch(_provider, ctx):
        check = get_db()
        try:
            row = check.execute(
                "SELECT metadata,write_started_at FROM messages "
                "WHERE thread_id='thread-1' AND input_order=1"
            ).fetchone()
            assert row["write_started_at"] is not None
            assert json.loads(row["metadata"])[
                "responsibility_map_snapshot"
            ] == snapshot
        finally:
            check.close()
        ctx.mark_accepted()
        await ctx.settle_delivery()
        return "fake", None, batch.identity

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(send_dispatch, "dispatch", dispatch)

    result = await execution.execute_claimed_batch(
        batch,
        thread_row=prepared.thread_row,
        target=prepared.target,
    )

    assert result.driver == "fake"


@pytest.mark.asyncio
async def test_preparation_failure_terminalizes_turn_and_input(
    isolated_db, monkeypatch,
) -> None:
    """No failure before provider I/O can leave a running owner behind."""
    from infra.database import get_db
    from infra.thread_driver import admit_input
    import infra.thread_execution as execution

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    db.close()
    assert admission.batch is not None

    async def _fail(*_args, **_kwargs):
        raise ValueError("bad preparation")

    monkeypatch.setattr(execution, "_prepare_batch", _fail)
    with pytest.raises(ValueError, match="bad preparation"):
        await execution.execute_claimed_batch(
            admission.batch,
            thread_row={"id": "thread-1", "user_id": "user-1"},
            target=SimpleNamespace(catalog_id="native/test", harness="native"),
        )

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("error", "bad preparation")
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()) == ("failed", "proved_no_write")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_archived_after_preflight_never_reaches_provider(
    isolated_db, monkeypatch,
) -> None:
    """The write fence rechecks lifecycle inside its storage transaction."""
    from infra.database import get_db
    from infra.send_types import SendRejected
    from infra.thread_driver import admit_input
    import infra.thread_execution as execution

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    db.execute(
        "UPDATE threads SET lifecycle_state='archived',archived_reason='user' "
        "WHERE id='thread-1'"
    )
    db.commit()
    db.close()
    assert admission.batch is not None

    async def prepare(*_args, **_kwargs):
        return SimpleNamespace(provider="native", batch=None)

    async def dispatch(*_args, **_kwargs):
        pytest.fail("archived input reached provider dispatch")

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(execution, "_dispatch_prepared", dispatch)

    with pytest.raises(SendRejected, match="thread is archived"):
        await execution.execute_claimed_batch(
            admission.batch,
            thread_row={"id": "thread-1", "user_id": "user-1"},
            target=SimpleNamespace(catalog_id="native/test", harness="native"),
        )

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("error", "thread is archived")
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()) == ("failed", "proved_no_write")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_target_resolution_failure_cannot_leave_claimed_turn_running(
    isolated_db, monkeypatch,
) -> None:
    """A bad destination after FIFO claim still settles the exact Turn."""
    from infra.database import get_db
    from infra.send_target import TargetInvalid
    from infra.thread_driver import admit_input
    import infra.thread_execution as execution

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','missing/model','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    db.close()
    assert admission.batch is not None

    def _fail(_model, **_kwargs):
        raise ValueError("model disappeared")

    monkeypatch.setattr("llm.resolution.resolve_execution_target", _fail)
    with pytest.raises(TargetInvalid, match="model disappeared"):
        await execution.drive_claimed_batch(admission.batch)

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()) == ("error", "model disappeared")
        assert db.execute(
            "SELECT delivery_state FROM messages WHERE id='input-1'"
        ).fetchone()[0] == "failed"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unavailable_target_holds_claimed_input_and_ends_turn(
    isolated_db, monkeypatch,
) -> None:
    """A provider-unavailable selection preserves its exact FIFO head."""
    from infra.database import get_db
    import infra.thread_execution as execution
    from tests.unavailable_model_support import admit_held_candidate, seed_model

    seed_model("omlx@srv/m", available=0)
    batch = admit_held_candidate("unavailable-thread", "omlx@srv/m", "unavailable-input")
    monkeypatch.setattr(execution, "drive_idle_thread", AsyncMock())

    result = await execution.drive_claimed_batch(batch)

    assert result.receipt.message_id == "unavailable-input"
    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='unavailable-input-turn'"
        ).fetchone()) == ("error", "selected_model_unavailable")
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome,delivery_error,active_turn_id "
            "FROM messages WHERE id='unavailable-input'"
        ).fetchone()) == (
            "queued", "proved_no_write", "selected_model_unavailable", None,
        )
    finally:
        db.close()
