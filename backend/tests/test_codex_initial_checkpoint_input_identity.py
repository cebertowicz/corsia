"""Codex initial checkpoint replacement preserves admitted input identity."""

from __future__ import annotations

from llm.context_window import WindowFacts
from types import SimpleNamespace

import pytest


THREAD_ID = "codex-initial-checkpoint-input"
TURN_ID = "turn-codex-initial-checkpoint-input"
MESSAGE_ID = "input-codex-initial-checkpoint-input"
ATTEMPT_ID = "attempt-codex-initial-checkpoint-input"
CATALOG_MODEL = "codex/gpt-5.6-luna"
TRANSPORT_MODEL = "gpt-5.6-luna"
CURRENT_INPUT = "preserve this admitted FIFO input"
INITIAL_PROVIDER_TEXT = "frozen initial provider payload"
CHECKPOINT_PROVIDER_TEXT = "frozen checkpoint-tail provider payload"


def _target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=CATALOG_MODEL,
        harness="codex",
        provider="codex",
        backend="codex",
        transport_model=TRANSPORT_MODEL,
        expected_reported_model=TRANSPORT_MODEL,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name=TRANSPORT_MODEL,
        supports_reasoning=True,
    )


def _reconstruction(provider_text: str):
    snapshot = SimpleNamespace(
        to_dict=lambda: {"snapshot_id": provider_text},
        source_inventory=(),
    )
    return SimpleNamespace(
        task_text=provider_text,
        turn_text=provider_text,
        snapshot=snapshot,
        snapshot_id=provider_text,
        history_format_version=1,
        history_start_exclusive=None,
        history_sealed_boundary=None,
        history_full_raw_replay_available=True,
        history_items=(),
        checkpoint_text=None,
    )


def _prepared_context(
    *,
    mode: str,
    provider_text: str,
    current_user_text: str,
):
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_preflight import attach_destination_fit_ledger
    from subagents.codex_destination import build_prepared_codex_destination

    destination = build_prepared_codex_destination(
        thread_id=THREAD_ID,
        target_catalog_id=CATALOG_MODEL,
        current_user_text=current_user_text,
        reconstruction=_reconstruction(provider_text),
        delivery_mode=mode,
        handle_fingerprint=None,
        tool_registry=object(),
        dynamic_tools=(),
    )
    rendered = RenderedDestinationPayload(
        payload=destination,
        input_tokens=destination.input_tokens,
        accounting_payload=destination.accounting_payload,
        fit_section_tokens=dict(destination.fit_section_tokens),
        fit_input_identity=MESSAGE_ID,
        fit_current_input=destination.current_user_text,
        fit_components={
            "fixed": destination.provider_text,
            "checkpoint": None,
            "tail": list(destination.history_items),
            "current_input": destination.current_user_text,
            "tools": list(destination.dynamic_tools),
        },
    )
    prepared = PreparedContextPayload(
        mode=mode,
        target_catalog_id=CATALOG_MODEL,
        model_context_window=200_000,
        history_epoch=1 if mode == "checkpoint_tail" else 0,
        sealed_boundary=None,
        replay=SimpleNamespace(
            mode=mode,
            checkpoint_generation_id=(
                "checkpoint-generation" if mode == "checkpoint_tail" else None
            ),
        ),
        rendered=rendered,
        runtime_context_epoch=6,
    )
    return attach_destination_fit_ledger(
        prepared,
        harness="codex",
        proven_target_window_tokens=200_000,
    )


def _admit_input():
    from infra.database import get_db
    from infra.input_writes import begin_batch_write
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (THREAD_ID, "user-1", CATALOG_MODEL, "t0", "t0"),
        )
        db.commit()
        admission = admit_input(
            THREAD_ID,
            role="user",
            content=CURRENT_INPUT,
            intent="apply_now",
            message_id=MESSAGE_ID,
            turn_id=TURN_ID,
            boot_epoch="boot-1",
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        begin_batch_write(
            admission.batch,
            attempt_id=ATTEMPT_ID,
            started_at="t2",
            db=db,
        )
        return admission.batch
    finally:
        db.close()


def _evidence():
    return SimpleNamespace(
        constructed=[],
        provider_writes=[],
        accepted=[],
        settlements=[],
        retired=[],
        sessions=[],
    )


class _ProviderSession:
    """Mock only Codex transport while preserving the real runtime transition."""

    returncode = None
    evidence = None

    def __init__(self, **kwargs) -> None:
        evidence = self.evidence
        assert evidence is not None
        self.binary = kwargs["binary"]
        self.working_dir = kwargs["working_dir"]
        self.project_id = kwargs["project_id"]
        self.effort = kwargs["effort"]
        self.codex_thread_id = f"provider-{len(evidence.sessions) + 1}"
        self.config_overrides = tuple(kwargs["config_overrides"])
        self.tool_registry = kwargs["tool_registry"]
        self.dynamic_tools = kwargs["dynamic_tools"]
        self.transport_generation_id = self.codex_thread_id
        self.active_turn_id = None
        self.request_log = []
        self.terminated: bool = False
        self.task = kwargs["task"]
        evidence.constructed.append(self.task)
        evidence.sessions.append(self)

    def set_callbacks(self, **_callbacks) -> None:
        return None

    async def start(
        self,
        *,
        on_turn_write_observed,
        on_turn_drain_confirmed,
        media_projection,
        client_message_id=None,
    ) -> bool:
        del media_projection, client_message_id
        evidence = self.evidence
        assert evidence is not None
        self.active_turn_id = f"turn-{self.codex_thread_id}"
        evidence.provider_writes.append(self.task)
        on_turn_write_observed()
        on_turn_drain_confirmed()
        return True

    async def terminate(self) -> None:
        self.terminated = True


async def _exercise_initial_replacement(
    monkeypatch,
    *,
    adopted_user_text: str,
    evidence,
):
    from infra.input_writes import ProviderWriteEvidence, settle_batch_write
    from subagents import codex_runtime
    from subagents import codex_spawn_preflight
    from subagents import harness_checkpoint_resume

    target = _target()
    batch = _admit_input()
    write_evidence = ProviderWriteEvidence()
    initial = _prepared_context(
        mode="exact_replay",
        provider_text=INITIAL_PROVIDER_TEXT,
        current_user_text=CURRENT_INPUT,
    )
    adopted = _prepared_context(
        mode="checkpoint_tail",
        provider_text=CHECKPOINT_PROVIDER_TEXT,
        current_user_text=adopted_user_text,
    )
    _ProviderSession.evidence = evidence

    async def maintain(_live, _thread_id, _harness, prepared, _refresh):
        assert prepared is initial
        return adopted

    async def refresh():
        return adopted

    async def settle_delivery() -> None:
        evidence.settlements.append(ATTEMPT_ID)
        settle_batch_write(ATTEMPT_ID, outcome="accepted")

    def mark_accepted() -> None:
        assert write_evidence.turn_owned
        write_evidence.mark_accepted()
        evidence.accepted.append(ATTEMPT_ID)

    async def settle_terminal(outcome: str, error: str | None) -> None:
        assert write_evidence.turn_owned
        write_evidence.mark_proved_no_write()
        settle_batch_write(
            ATTEMPT_ID, outcome="proved_no_write", error=error,
            no_write_state="cancelled" if outcome == "cancelled" else "failed",
        )

    monkeypatch.setattr(codex_spawn_preflight, "codex_connection", lambda _db: {})
    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession",
        _ProviderSession,
    )
    monkeypatch.setattr(
        harness_checkpoint_resume,
        "maintain_external_initial_boundary",
        maintain,
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, **_kwargs: target,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_retire_registered_thread_artifacts",
        lambda _thread_id, **kwargs: evidence.retired.append(
            kwargs.get("keep_thread_id")
        ),
    )

    from codex_runtime_fixtures import locked_spawn

    result = await locked_spawn(
        codex_runtime._spawn_turn,
        THREAD_ID,
        CURRENT_INPUT,
        successor_turn_id=TURN_ID,
        codex_thread_id=None,
        binary="codex",
        model=TRANSPORT_MODEL,
        catalog_model_id=CATALOG_MODEL,
        windows=WindowFacts(200_000, None),
        prepared_context=initial,
        refresh_prepared_context=refresh,
        mark_accepted=mark_accepted,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=write_evidence.transfer_to_turn,
        settle_delivery_terminal=settle_terminal,
        preadmitted_turn=batch.identity,
    )
    return result


def _reset_codex_runtime() -> None:
    from subagents import codex_runtime

    codex_runtime._LIVE.reset()
    codex_runtime._APP_SESSIONS.clear()
    codex_runtime._CODEX_THREADS.clear()
    _ProviderSession.evidence = None


@pytest.mark.asyncio
async def test_initial_checkpoint_replacement_preserves_original_input_and_payload(
    isolated_db,
    monkeypatch,
) -> None:
    """The adopted attempt sends once under the original FIFO input identity."""
    from infra.database import get_db
    from subagents import codex_runtime

    _reset_codex_runtime()
    evidence = _evidence()
    try:
        result = await _exercise_initial_replacement(
            monkeypatch,
            adopted_user_text=CURRENT_INPUT,
            evidence=evidence,
        )
        assert result.turn_id == TURN_ID
        assert evidence.constructed == [
            INITIAL_PROVIDER_TEXT,
            CHECKPOINT_PROVIDER_TEXT,
        ]
        assert evidence.provider_writes == [CHECKPOINT_PROVIDER_TEXT]
        assert evidence.accepted == [ATTEMPT_ID]
        assert evidence.settlements == [ATTEMPT_ID]
        assert evidence.sessions[0].terminated is True
        assert evidence.sessions[1].terminated is False
        assert evidence.retired == ["provider-2"]
        live = codex_runtime._LIVE.get(THREAD_ID)
        assert live is not None and live.corsia_turn == result

        db = get_db()
        try:
            turn_count = db.execute(
                "SELECT COUNT(*) FROM active_turns WHERE id=?",
                (TURN_ID,),
            ).fetchone()[0]
            message = db.execute(
                "SELECT delivery_state,write_outcome,active_turn_id "
                "FROM messages WHERE id=?",
                (MESSAGE_ID,),
            ).fetchone()
        finally:
            db.close()
        assert turn_count == 1
        assert tuple(message) == ("delivered", "accepted", TURN_ID)
    finally:
        _reset_codex_runtime()


@pytest.mark.asyncio
async def test_initial_checkpoint_replacement_rejects_changed_input_before_write(
    isolated_db,
    monkeypatch,
) -> None:
    """A refreshed destination for different input still fails before transport."""
    from infra.database import get_db
    from subagents.codex_runtime_errors import CodexPreparedContextStale
    from subagents.codex_runtime_terminal import CommittedTerminalFailure
    from core.turn_core import drive_turn_settlement

    _reset_codex_runtime()
    evidence = _evidence()
    try:
        with pytest.raises(CommittedTerminalFailure) as failure:
            await _exercise_initial_replacement(
                monkeypatch,
                adopted_user_text="different refreshed FIFO input",
                evidence=evidence,
            )
        assert isinstance(failure.value.publication_error, CodexPreparedContextStale)
        assert "does not match this thread/model/input" in str(failure.value.publication_error)
        assert failure.value.settlement is not None
        await drive_turn_settlement(failure.value.settlement)

        assert evidence.constructed == [INITIAL_PROVIDER_TEXT]
        assert evidence.provider_writes == []
        assert evidence.accepted == []
        assert evidence.settlements == []

        db = get_db()
        try:
            message = db.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id=?",
                (MESSAGE_ID,),
            ).fetchone()
            turn = db.execute(
                "SELECT status FROM active_turns WHERE id=?", (TURN_ID,),
            ).fetchone()
        finally:
            db.close()
        assert tuple(message) == ("failed", "proved_no_write")
        assert turn["status"] == "error"
    finally:
        _reset_codex_runtime()
