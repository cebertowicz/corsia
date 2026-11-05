"""Checkpoint folds read the verdict for their own target (#2477 slice 2c)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from connections import harness_usage
from core import checkpoint_runtime
from core.checkpoint_resume import ProviderBoundary, _start_checkpoint
from core.checkpoint_runtime import CheckpointTargetLimited, request_checkpoint
from core.checkpoint_types import CheckpointProviderForkRequest
from core.checkpoint_types import (
    CheckpointRequest,
    CheckpointUnavailable,
    IdleCheckpointDisposition,
    IdleCheckpointReason,
)
from core.checkpoint_warm_request import CheckpointAdmission
from infra.database import get_db
from llm.resolution import ExecutionTarget
from vendor_account_fixtures import default_id, default_receipt

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

SONNET = "claude-code/sonnet"
OPUS = "claude-code/opus"


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _target(model: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=model, harness="claude-code", provider="claude-code",
        backend="cli", transport_model=model.split("/")[-1],
        expected_reported_model=None, context_window=200_000, available=True,
        availability_reason=None, display_name=model, supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _seed(thread_id: str, model: str) -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,created_at,updated_at,next_message_seq) "
            "VALUES (?, 'user-1', ?, 0, 't0', 't0', 4)", (thread_id, model),
        )
        for seq in range(1, 4):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                "VALUES (?,?,?,'body','t0',?)",
                (f"{thread_id}-m{seq}", thread_id, "user" if seq == 1 else "assistant", seq),
            )
        db.commit()
    finally:
        db.close()


def _limit_sonnet(*, account_id: str | None = None, resets_at: str | None = None) -> None:
    """A Sonnet-scoped limit: Sonnet is limited, Opus on the same connection is not."""
    db = get_db()
    try:
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=resets_at or _future(),
            scope_model="sonnet", source="test", db=db,
            receipt=default_receipt("claude-code", db=db) if account_id is None else default_receipt_for(account_id, db=db),
        )
    finally:
        db.close()


def default_receipt_for(account_id: str, *, db):
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt("claude-code", account_id=account_id, db=db)
    assert receipt is not None
    return receipt


def _configure_rollover(thread_id: str, *, enabled: bool) -> tuple[str, str]:
    from connections.account_evidence import mark_discovery
    from connections.provider_policy import effective_policy, set_policy
    from connections.account_policy import select_account
    from connections.vendor_accounts import add_account, set_account_state
    from vendor_account_fixtures import default_id

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO models "
            "(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (SONNET, "Sonnet", SONNET, "claude-code", "sonnet", "claude-code", "t", "t"),
        )
        db.execute("UPDATE models SET context_window=200000 WHERE id=?", (SONNET,))
        db.commit()
        first = default_id("claude-code", db=db)
        second = add_account("claude-code", "Claude rollover")
        second = set_account_state(second["id"], "signed_in", expected_revision=0)
        for account_id in (first, second["id"]):
            receipt = default_receipt_for(account_id, db=db)
            assert mark_discovery(
                account_id, receipt=receipt, status="ready",
                models={SONNET: {"available": True, "context_window": 200_000}}, db=db,
            )
        policy = effective_policy(None, "claude-code", db=db)
        set_policy(
            None, "claude-code", rollover=enabled, order=[first, second["id"]],
            disabled=[], expected_revision=policy["revision"], db=db,
        )
        db.commit()
        from infra.database import write_transaction

        with write_transaction(db, scope="configure_rollover_account"):
            harness_usage.record_allowed(
                provider="claude-code", receipt=default_receipt_for(second["id"], db=db),
                source="discovery", db=db,
            )
        select_account(thread_id, "claude-code", account_id=first, db=db)
        db.commit()
        return first, second["id"]
    finally:
        db.close()


def _pair(
    thread_id: str, checkpoint_id: str, target_model: str, *,
    trigger: str = "pressure", turn_id: str | None = "turn-1",
) -> tuple[CheckpointRequest, CheckpointProviderForkRequest]:
    request = CheckpointRequest(
        id=checkpoint_id, thread_id=thread_id, history_epoch=0,
        previous_generation_id=None, through_seq=3,
        through_message_id=f"{thread_id}-m3", trigger=trigger, mode="warm",
        source_harness="claude-code", source_model=target_model,
        writer_harness="claude-code", writer_model=target_model,
        originating_turn_id=turn_id, first_included_seq=1, started_at="t1",
    )
    provider = CheckpointProviderForkRequest(
        fork_id=checkpoint_id, thread_id=thread_id,
        target=_target(target_model), instruction="fold", reasoning_effort=None,
        working_dir="/tmp", warm_source=object(),
    )
    return request, provider


def _checkpoint_rows() -> int:
    db = get_db()
    try:
        return db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    finally:
        db.close()


@pytest.fixture
def launch(monkeypatch) -> Mock:
    mock = Mock()
    monkeypatch.setattr(checkpoint_runtime, "_launch_checkpoint", mock)
    return mock


@pytest.mark.asyncio
async def test_limited_target_is_refused_before_any_row_or_provider_call(isolated_db, launch):
    _seed("t1", SONNET)
    _limit_sonnet()
    request, provider = _pair("t1", "c1", SONNET)

    with pytest.raises(CheckpointTargetLimited) as raised:
        await request_checkpoint(request, provider)

    assert "account limit on claude-code" in str(raised.value)
    assert _checkpoint_rows() == 0
    launch.assert_not_called()


def test_model_switch_target_checkpoint_freezes_the_turn_claim_account(isolated_db, monkeypatch):
    from core.checkpoint_warm_request import CheckpointAdmission
    from thread import model_checkpoint

    _seed("switch-preparation", SONNET)
    first, second = _configure_rollover("switch-preparation", enabled=True)
    _limit_sonnet(account_id=first)
    checkpoint, provider = _pair("switch-preparation", "c-preparation", SONNET, trigger="model_switch")
    context = SimpleNamespace(
        source_target=None, destination_target=_target(SONNET), boundary=None,
        previous=None, projection=None, working_dir="/tmp", reasoning_effort=None,
        destination_binary=None, destination_mcp_config=None,
    )
    switch = SimpleNamespace(thread_id="switch-preparation", source_model=SONNET)
    monkeypatch.setattr(model_checkpoint, "read_switch_checkpoint_context", lambda _request: context)
    monkeypatch.setattr(
        "core.checkpoint_cold_admission.build_checkpoint_admission",
        lambda **_kwargs: CheckpointAdmission(checkpoint, provider),
    )

    admission = model_checkpoint._target_cold_admission(switch, "c-preparation")

    assert admission.provider_request.vendor_account_id == second
    assert admission.provider_request.vendor_account_bound is True


@pytest.mark.asyncio
async def test_target_checkpoint_claims_ready_rollover_account(isolated_db, launch):
    from core.fork_account import admit_fork

    _seed("rollover", SONNET)
    first, second = _configure_rollover("rollover", enabled=True)
    _limit_sonnet(account_id=first)
    request, provider = _pair("rollover", "c-rollover", SONNET)
    from connections.account_policy import turn_account

    db = get_db()
    try:
        assert turn_account("claude-code", thread_id="rollover", model=SONNET, db=db) == (second, None)
    finally:
        db.close()

    provider = replace(provider, vendor_account_id=second, vendor_account_bound=True)
    record = await request_checkpoint(request, provider)

    assert record.id == "c-rollover"
    launched = launch.call_args.args[1]
    assert admit_fork(launched).vendor_account_id == second


@pytest.mark.asyncio
async def test_unbound_fold_holds_on_limited_chosen_account_even_with_a_ready_rollover_account(
    isolated_db, launch,
):
    """A pressure fold launches on the chosen account, so the gate checks that one."""
    from core.fork_account import admit_fork

    _seed("unbound-rollover", SONNET)
    first, _second = _configure_rollover("unbound-rollover", enabled=True)
    _limit_sonnet(account_id=first)
    request, provider = _pair("unbound-rollover", "c-unbound-rollover", SONNET)
    assert provider.vendor_account_bound is False
    assert admit_fork(provider).vendor_account_id == first

    with pytest.raises(CheckpointTargetLimited):
        await request_checkpoint(request, provider)

    assert _checkpoint_rows() == 0
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_target_checkpoint_holds_on_limited_chosen_account_when_rollover_is_off(isolated_db, launch):
    _seed("no-rollover", SONNET)
    first, _second = _configure_rollover("no-rollover", enabled=False)
    _limit_sonnet(account_id=first)
    request, provider = _pair("no-rollover", "c-no-rollover", SONNET)

    with pytest.raises(CheckpointTargetLimited):
        await request_checkpoint(request, provider)

    launch.assert_not_called()


@pytest.mark.asyncio
async def test_target_checkpoint_holds_with_reset_when_all_rollover_accounts_are_limited(isolated_db, launch):
    from infra.held_admission import limited_admission
    from infra.thread_input_types import held_admission_line

    _seed("all-limited", SONNET)
    first, second = _configure_rollover("all-limited", enabled=True)
    _limit_sonnet(account_id=first, resets_at="2099-01-01T00:00:00+00:00")
    _limit_sonnet(account_id=second, resets_at="2099-02-01T00:00:00+00:00")
    request, provider = _pair("all-limited", "c-all-limited", SONNET)

    with pytest.raises(CheckpointTargetLimited) as raised:
        await request_checkpoint(request, provider)

    held = limited_admission("all-limited", SONNET)
    assert held is not None
    assert held.resets_at == "2099-01-01T00:00:00+00:00"
    assert str(raised.value) == held_admission_line(held)
    assert "resets " in str(raised.value)
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_gate_reads_the_fork_target_not_the_thread_model(isolated_db, launch):
    _seed("bound-opus", OPUS)
    _seed("bound-sonnet", SONNET)
    _limit_sonnet()

    # Thread bound to an allowed model; the fork targets the limited one.
    request, provider = _pair("bound-opus", "c-limited", SONNET)
    with pytest.raises(CheckpointTargetLimited):
        await request_checkpoint(request, provider)

    # Thread bound to the limited model; the fork targets an allowed one.
    request, provider = _pair("bound-sonnet", "c-allowed", OPUS)
    record = await request_checkpoint(request, provider)

    assert record.id == "c-allowed"
    launch.assert_called_once()


@pytest.mark.parametrize("state", ["allowed", "unknown"])
@pytest.mark.asyncio
async def test_allowed_and_unknown_targets_launch_unchanged(isolated_db, launch, state):
    _seed("t1", SONNET)
    if state == "allowed":
        db = get_db()
        try:
            harness_usage.record_allowed(provider="claude-code", source="test", db=db, receipt=default_receipt("claude-code", db=db))
        finally:
            db.close()
    request, provider = _pair("t1", "c1", SONNET)

    record = await request_checkpoint(request, provider)

    assert record.id == "c1"
    launch.assert_called_once()


@pytest.mark.asyncio
async def test_native_target_fold_launches_without_an_account(isolated_db, launch):
    _seed("t1", "native/mock-model")
    request, provider = _pair("t1", "c1", SONNET)
    request = replace(request, source_harness="native", source_model="native/mock-model",
                      writer_harness="native", writer_model="native/mock-model")
    native = replace(provider.target, catalog_id="native/mock-model", harness="native", provider="mock")

    record = await request_checkpoint(request, replace(provider, target=native))

    assert record.id == "c1"
    launch.assert_called_once()


@pytest.mark.asyncio
async def test_an_active_fold_coalesces_before_the_gate(isolated_db, launch):
    _seed("t1", SONNET)
    first, first_provider = _pair("t1", "running", SONNET)
    await request_checkpoint(first, first_provider)
    _limit_sonnet()
    second, second_provider = _pair("t1", "second", SONNET)

    record = await request_checkpoint(second, second_provider)

    assert record.id == "running"
    launch.assert_called_once()


def _boundary(thread_id: str, prepare) -> ProviderBoundary:
    return ProviderBoundary(
        thread_id=thread_id, turn=SimpleNamespace(turn_id="turn-1", turn_order=1),
        context_window=200_000, canonical_input_tokens=1, provider_input_tokens=1,
        minimum_input_tokens=1, adopted_checkpoint_id=None, active_checkpoint=None,
        installed_checkpoint=None, prepare_checkpoint=prepare,
        render_installed=AsyncMock(), wait_scope=SimpleNamespace(checkpoint_id=None),
    )


def _preparer(thread_id: str, model: str):
    async def prepare(checkpoint_id: str, required: bool):
        request, provider = _pair(thread_id, checkpoint_id, model)
        return CheckpointAdmission(request, provider)

    return prepare


@pytest.mark.asyncio
async def test_optional_pressure_fold_skips_and_required_fails_unavailable(isolated_db, launch):
    _seed("t1", SONNET)
    _limit_sonnet()
    boundary = _boundary("t1", _preparer("t1", SONNET))

    assert await _start_checkpoint(boundary, required=False) is None
    with pytest.raises(CheckpointUnavailable) as raised:
        await _start_checkpoint(boundary, required=True)

    assert raised.value.limited_target == SONNET
    assert raised.value.limited_connection == "claude-code"
    assert "account limit on claude-code" in str(raised.value)
    assert _checkpoint_rows() == 0
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_initial_external_fold_goes_through_its_real_caller(isolated_db, launch, monkeypatch):
    from subagents import harness_checkpoint_resume as external

    _seed("t1", SONNET)
    _limit_sonnet()
    context = SimpleNamespace(
        target=_target(SONNET), boundary=SimpleNamespace(
            thread_id="t1", history_epoch=0, seq=3, message_id="t1-m3",
        ),
        previous_generation_id=None, working_dir="/tmp", reasoning_effort=None,
        binary="claude",
    )
    monkeypatch.setattr(
        "core.fork_render.capture_warm_fork_source",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "core.fork_cc_boundary.capture_boundary_warm_source",
        AsyncMock(return_value=object()),
    )

    async def prepare(checkpoint_id: str, required: bool):
        return await external._prepare_initial_external_checkpoint(
            checkpoint_id, required, context=context, live=object(),
            thread_id="t1", identity=SimpleNamespace(turn_id="turn-1", turn_order=1),
            prepared=SimpleNamespace(mode="provider_resume"),
        )

    boundary = _boundary("t1", prepare)
    assert await _start_checkpoint(boundary, required=False) is None
    with pytest.raises(CheckpointUnavailable) as raised:
        await _start_checkpoint(boundary, required=True)

    assert raised.value.limited_target == SONNET
    assert _checkpoint_rows() == 0
    launch.assert_not_called()


def _completed_marker(thread_id: str, turn_id: str, model: str) -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
            "VALUES (?,?,0,?,'completed','s',datetime('now'),'boot',1)",
            (turn_id, thread_id, model),
        )
        db.commit()
    finally:
        db.close()


def _pending(turn_id: str) -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()[0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_idle_fold_on_a_limited_target_consumes_the_marker_as_a_skip(
    isolated_db, launch, monkeypatch,
):
    _seed("t1", SONNET)
    _completed_marker("t1", "turn-idle", SONNET)
    _limit_sonnet()
    monkeypatch.setattr(
        checkpoint_runtime, "fail_idle_checkpoint",
        AsyncMock(side_effect=AssertionError("a skip is never a failed attempt")),
    )
    request, provider = _pair("t1", "idle-c", SONNET, trigger="idle", turn_id="turn-idle")

    result = await checkpoint_runtime.request_idle_checkpoint(
        "turn-idle", CheckpointAdmission(request, provider),
    )

    assert result.disposition is IdleCheckpointDisposition.REFUSED
    assert result.reason is IdleCheckpointReason.TARGET_LIMITED
    assert _pending("turn-idle") == 0  # consumed, never re-armed (amendment 14)
    assert _checkpoint_rows() == 0
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_idle_fold_on_an_allowed_target_still_fires(isolated_db, launch):
    _seed("t1", OPUS)
    _completed_marker("t1", "turn-idle", OPUS)
    _limit_sonnet()
    request, provider = _pair("t1", "idle-c", OPUS, trigger="idle", turn_id="turn-idle")

    result = await checkpoint_runtime.request_idle_checkpoint(
        "turn-idle", CheckpointAdmission(request, provider),
    )

    assert result.disposition is IdleCheckpointDisposition.ADMITTED
    launch.assert_called_once()


def _switch_request(thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(thread_id=thread_id, operation_id="op")


@pytest.mark.asyncio
async def test_switch_warm_rung_falls_to_cold_and_cold_rung_surfaces_to_driver(
    isolated_db, launch, monkeypatch, caplog,
):
    from loguru import logger

    from thread import model_checkpoint, model_pending

    _seed("t1", SONNET)
    _limit_sonnet()
    request = _switch_request("t1")

    async def warm(_request, checkpoint_id):
        return CheckpointAdmission(*_pair("t1", checkpoint_id, SONNET, trigger="model_switch"))

    async def cold(_request, checkpoint_id):
        return CheckpointAdmission(*_pair("t1", checkpoint_id, SONNET, trigger="model_switch"))

    monkeypatch.setattr(model_checkpoint, "prepare_source_checkpoint", warm)
    monkeypatch.setattr(model_checkpoint, "prepare_target_checkpoint", cold)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}")
    try:
        assert await model_pending._start_checkpoint(request, "warm") == (None, False)
    finally:
        logger.remove(sink)
    assert any("cause=source_target_limited" in line for line in lines)

    with pytest.raises(CheckpointTargetLimited):
        await model_pending._start_checkpoint(request, "cold")

    monkeypatch.setattr(model_pending, "_checkpoint_state", lambda _request: (None, None))
    with pytest.raises(CheckpointTargetLimited):
        await model_pending._resolve_required_checkpoint(request)
    assert _checkpoint_rows() == 0
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_stop_while_waiting_on_a_fold_never_reads_the_gate(isolated_db, monkeypatch):
    from core.checkpoint_wait import await_checkpoint_or_stop

    _seed("t1", SONNET)
    _limit_sonnet()
    gate = Mock(side_effect=AssertionError("Stop must not consult the fold gate"))
    monkeypatch.setattr("infra.held_admission.limited_admission", gate)
    from core.checkpoint_wait import CheckpointWaitScope

    scope = CheckpointWaitScope()
    scope.request_stop()
    cancelled = AsyncMock()

    async def never(_checkpoint_id: str):
        await asyncio.sleep(3600)

    with pytest.raises(asyncio.CancelledError):
        await await_checkpoint_or_stop(scope, "running-fold", never, on_stop=cancelled)

    cancelled.assert_awaited_once_with(scope)
    gate.assert_not_called()


def _refused_input(thread_id: str, model: str, *, write_started: bool = True):
    from infra.input_writes import begin_batch_write
    from infra.thread_driver import admit_input

    db = get_db()
    for model_id in (SONNET, OPUS):
        db.execute(
            "INSERT OR IGNORE INTO models (id,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,'claude-code',?,?,?,1000000,'t0','t0')",
            (model_id, model_id, model_id, model_id),
        )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user-1', ?, 't0', 't0')", (thread_id, model),
    )
    db.commit()
    admission = admit_input(
        thread_id, role="user", content="answer me", intent="apply_now",
        message_id=f"{thread_id}-input", turn_id=f"{thread_id}-turn",
        boot_epoch="boot", admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    if write_started:
        begin_batch_write(
            admission.batch, attempt_id=f"{thread_id}-attempt", started_at="t2", db=db,
        )
    return db


def _limited_error(target: str, admission_model: str) -> CheckpointUnavailable:
    """The refusal the gate raises, with the window settlement will re-resolve."""
    from llm.context_window import resolve_window_for_row
    from llm.resolution import _target_row

    db = get_db()
    try:
        window = resolve_window_for_row(_target_row(admission_model, db=db), db=db)
    finally:
        db.close()
    return CheckpointUnavailable(
        "Held: account limit on claude-code.", context_window=window,
        installed_checkpoint_id=None, limited_target=target,
        limited_connection="claude-code",
    )


def _state(db, thread_id: str):
    row = db.execute(
        "SELECT delivery_state,write_outcome,delivery_error FROM messages WHERE id=?",
        (f"{thread_id}-input",),
    ).fetchone()
    return row["delivery_state"], row["write_outcome"], row["delivery_error"]


def test_same_target_required_fold_leaves_the_input_held_with_a_deadline(isolated_db):
    from infra.thread_driver import finish_turn
    from infra.thread_input_types import HeldAdmission

    db = _refused_input("t1", SONNET, write_started=False)
    try:
        _limit_sonnet()
        terminal = finish_turn(
            "t1-turn", outcome="error", ended_at="t3", successor_turn_id="next",
            boot_epoch="boot", error=_limited_error(SONNET, SONNET), db=db,
        )

        assert _state(db, "t1") == ("queued", None, None)  # released, never failed
        assert terminal.failed_message_ids == ()
        assert isinstance(terminal.successor, HeldAdmission)
        verdict = harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["vendor_verdicts"]["sonnet"]
        assert terminal.successor.resets_at == verdict["resets_at"]  # the deadline lives on the verdict
        assert "release" not in verdict
        assert db.execute("SELECT COUNT(*) FROM automation_schedules").fetchone()[0] == 0
    finally:
        db.close()


def test_differing_target_required_fold_fails_visibly_once(isolated_db):
    from infra.input_writes import settle_batch_write

    db = _refused_input("t1", OPUS)  # admission model is not limited
    try:
        _limit_sonnet()
        settle_batch_write(
            "t1-attempt", outcome="proved_no_write", db=db,
            error=_limited_error(SONNET, OPUS),
        )

        state, outcome, error = _state(db, "t1")
        assert (state, outcome) == ("failed", "proved_no_write")
        assert "account limit" in error
    finally:
        db.close()


def test_differing_target_with_limited_admission_model_is_released_to_its_hold(isolated_db):
    from infra.input_writes import settle_batch_write

    db = _refused_input("t1", SONNET)
    try:
        _limit_sonnet()
        settle_batch_write(
            "t1-attempt", outcome="proved_no_write", db=db,
            error=_limited_error(OPUS, SONNET),
        )

        assert _state(db, "t1") == ("queued", None, None)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_manual_api_and_agent_tool_refuse_with_a_named_reason_and_no_row(
    isolated_db, launch, monkeypatch,
):
    from fastapi import HTTPException

    from api import checkpoints
    from tools.request_checkpoint import RequestCheckpointTool

    _seed("t1", SONNET)
    _limit_sonnet()
    request, provider = _pair("t1", "c-manual", SONNET, trigger="manual", turn_id=None)
    monkeypatch.setattr(checkpoints, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(checkpoints, "_owned_active", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(checkpoints, "_prepare_manual", AsyncMock(return_value=(request, provider)))

    with pytest.raises(HTTPException) as raised:
        await checkpoints.request_manual_checkpoint("t1", "token")

    assert raised.value.status_code == 409
    assert raised.value.detail["reason"] == "target_limited"

    async def native(*_args, **_kwargs):
        return None

    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", native)
    monkeypatch.setattr(
        "core.checkpoint_agent_admission.prepare_external_checkpoint",
        AsyncMock(return_value=(request, provider)),
    )
    monkeypatch.setattr("tools.request_checkpoint._caller_user_id", lambda _thread: "user-1")
    message = await RequestCheckpointTool().execute(reason="boundary", _thread_id="t1")

    assert "not available right now" in message
    assert _checkpoint_rows() == 0
    launch.assert_not_called()
