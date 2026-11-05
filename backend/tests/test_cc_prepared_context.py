from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import json
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.cc_runtime_fixtures import cc_target, settle_test_input
from tests.vendor_account_fixtures import point_default_home


@pytest.fixture(autouse=True)
def _default_claude_root(monkeypatch, tmp_path, request):
    """The Default account's root, which renders and launches here resolve to."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-home"))
    if "main_db" in request.fixturenames:
        request.getfixturevalue("main_db")
        point_default_home("claude-code", tmp_path / "cc-home")


def _register_live_provider(main_db, provider_session_id: str) -> str:
    """Give a live test handle the durable provider identity production owns."""
    from registry.service import register_cc_session
    from subagents.cc_runtime_helpers import _main_working_dir

    row = main_db.execute(
        "SELECT project_id FROM threads WHERE id='main-1'"
    ).fetchone()
    working_dir = _main_working_dir(row["project_id"], main_db)
    register_cc_session(
        "main-1", provider_session_id, cwd=working_dir, db=main_db,
    )
    return working_dir


def _projection(
    *messages: tuple[str, int, str, str],
    start_exclusive=None,
):
    from history.types import (
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )

    portable_messages = tuple(
        PortableMessage(
            message_id=message_id,
            seq=seq,
            turn_id="prepared-turn",
            role=role,
            content=content,
            created_at=f"2026-07-16T00:00:{seq:02d}Z",
        )
        for message_id, seq, role, content in messages
    )
    turns: tuple[PortableTurn, ...]
    if portable_messages:
        from history.types import HistoryBoundary

        sealed_boundary = HistoryBoundary(
            thread_id="main-1",
            history_epoch=0,
            seq=portable_messages[-1].seq,
            message_id=portable_messages[-1].message_id,
        )
        turns = (
            PortableTurn(
                turn_id="prepared-turn",
                status="completed",
                messages=portable_messages,
                boundary=sealed_boundary,
            ),
        )
    else:
        sealed_boundary = start_exclusive
        turns = ()
    return PortableHistoryProjection(
        format_version=1,
        thread_id="main-1",
        history_epoch=0,
        start_exclusive=start_exclusive,
        sealed_boundary=sealed_boundary,
        turns=turns,
        full_raw_replay_available=True,
    )


def _prepared_context(
    projection,
    *,
    mode: str,
    checkpoint_json: str | None = None,
):
    from core.context_payload import PreparedContextPayload, RenderedDestinationPayload
    from core.context_replay import ReplaySelection

    checkpoint_boundary = projection.start_exclusive
    replay = ReplaySelection(
        mode=mode,
        history_epoch=projection.history_epoch,
        sealed_boundary=projection.sealed_boundary,
        tail=projection,
        checkpoint_generation_id=(
            "checkpoint-generation" if mode == "checkpoint_tail" else None
        ),
        checkpoint_boundary=(
            checkpoint_boundary if mode == "checkpoint_tail" else None
        ),
        checkpoint_json=checkpoint_json,
    )
    return PreparedContextPayload(
        mode="exact_replay" if mode == "exact" else mode,
        target_catalog_id="claude-code/opus",
        model_context_window=200_000,
        history_epoch=projection.history_epoch,
        sealed_boundary=projection.sealed_boundary,
        replay=replay,
        rendered=RenderedDestinationPayload(payload={}, input_tokens=1),
    )


def _prepared_provider_resume(projection):
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )

    return PreparedContextPayload(
        mode="provider_resume",
        target_catalog_id="claude-code/opus",
        model_context_window=200_000,
        history_epoch=projection.history_epoch,
        sealed_boundary=projection.sealed_boundary,
        replay=None,
        rendered=RenderedDestinationPayload(payload={}, input_tokens=1),
    )


def _with_rendered_destination(prepared, destination):
    from core.context_payload import RenderedDestinationPayload
    from core.destination_preflight import attach_destination_fit_ledger

    rendered = replace(
        prepared,
        rendered=RenderedDestinationPayload(
            payload=destination,
            input_tokens=destination.input_tokens,
            source_tokens=destination.source_tokens,
            source_section_tokens=destination.source_section_tokens,
            accounting_payload=destination.accounting_payload,
            fit_section_tokens=dict(destination.fit_section_tokens),
            fit_input_identity="prepared-test-input",
            fit_current_input=destination.frame_reconstruction.turn_text,
            fit_components={
                "fixed": (
                    destination.seed_reconstruction.seed_text
                    if destination.seed_reconstruction is not None
                    else destination.live_handle_fingerprint
                ),
                "checkpoint": destination.checkpoint_text,
                "tail": (
                    destination.transcript.content
                    if destination.transcript is not None
                    else None
                ),
                "current_input": destination.frame_reconstruction.turn_text,
                "tools": list(destination.tool_definitions),
            },
        ),
    )
    return attach_destination_fit_ledger(
        rendered,
        harness="claude-code",
        proven_target_window_tokens=prepared.model_context_window,
    )


def _proved_sequence_context(**attributes):
    """Build a minimal genuine fit proof for runtime-ordering tests."""
    from core.destination_fit import build_destination_fit_ledger

    accounting_payload = {"prepared": "sequence-test"}
    current_input = "sequence-test-input"
    sections = {
        "fixed": 1,
        "checkpoint": 0,
        "tail": 0,
        "current_input": 1,
        "tools": 0,
        "provider_wrapper": 0,
    }
    rendered = SimpleNamespace(
        accounting_payload=accounting_payload,
        fit_input_identity="sequence-test-head",
        fit_current_input=current_input,
    )
    ledger = build_destination_fit_ledger(
        harness="claude-code",
        target_catalog_id="claude-code/opus",
        proven_target_window_tokens=200_000,
        accounting_payload=accounting_payload,
        rendered_input_tokens=sum(sections.values()),
        section_tokens=sections,
        input_identity=rendered.fit_input_identity,
        current_input=current_input,
        components={"prepared": "sequence-test"},
    )
    return SimpleNamespace(
        rendered=rendered,
        fit_ledger=ledger,
        **attributes,
    )


def _seed_decoy_history(db) -> None:
    from infra.queries import insert_message

    insert_message(
        "main-1",
        "user",
        "DB_DECOY_MUST_NOT_REPLACE_PREPARED_REPLAY",
        {"turn_unbound_reason": "legacy_import"},
        msg_id="db-decoy",
        db=db,
        created_at="2026-07-16T00:00:00Z",
    )
    db.commit()


async def _feed_main(
    text: str,
    *,
    prepared_context,
    binary: str,
    claude_dir: str,
) -> None:
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    from subagents import cc_runtime

    turn_id = str(uuid.uuid4())
    admission = admit_input(
        "main-1",
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"prepared-input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    assert admission.batch is not None

    async def persist_input_once() -> None:
        return None

    point_default_home("claude-code", claude_dir)  # an account launches only in its own home
    with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
        await cc_runtime.feed_main(
            "main-1",
            text,
            target=cc_target(),
            turn_id=turn_id,
            binary=binary,
            claude_dir=claude_dir,
            broadcast=lambda _thread_id, _payload: None,
            prepared_context=prepared_context,
            persist_input_once=persist_input_once,
            preadmitted_turn=admission.batch.identity,
            mark_accepted=lambda: settle_test_input(admission.batch),
        )


@pytest.mark.asyncio
async def test_cold_cc_reconstruction_consumes_prepared_exact_replay(
    main_db,
    fake_claude,
    tmp_path,
):
    from subagents import cc_runtime

    _seed_decoy_history(main_db)
    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "PREPARED_EXACT_USER"),
            ("prepared-answer", 2, "assistant", "PREPARED_EXACT_ANSWER"),
        ),
        mode="exact",
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="current user turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)

    await _feed_main(
        "current user turn",
        prepared_context=prepared,
        binary=fake_claude,
        claude_dir=str(tmp_path / "cc-home"),
    )

    live = cc_runtime._LIVE.get("main-1")
    transcript = open(
        live.session.reconstructed_transcript_path,
        encoding="utf-8",
    ).read()
    seed = open(live.session.seed_file, encoding="utf-8").read()
    assert "PREPARED_EXACT_USER" in transcript
    assert "PREPARED_EXACT_ANSWER" in transcript
    assert "DB_DECOY_MUST_NOT_REPLACE_PREPARED_REPLAY" not in transcript
    assert "[CORSIA portable checkpoint]" not in seed
    await cc_runtime.retire("main-1")


def test_cc_switch_render_accepts_only_the_explicit_source_model(
    main_db,
    tmp_path,
):
    from subagents import cc_runtime

    source_model = "anthropic/claude-3-5-sonnet"
    main_db.execute(
        "UPDATE threads SET model=? WHERE id='main-1'",
        (source_model,),
    )
    main_db.commit()
    prepared = _prepared_context(_projection(), mode="exact")

    with pytest.raises(ValueError, match="source model changed"):
        cc_runtime.prepare_cc_destination_payload(
            "main-1",
            target=cc_target(),
            current_user_text="",
            selection=prepared.replay,
            provider_resume=False,
            is_subagent=False,
            db=main_db,
        )

    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="",
        selection=prepared.replay,
        provider_resume=False,
        expected_selected_model=source_model,
        is_subagent=False,
        db=main_db,
    )

    assert destination.target_catalog_id == "claude-code/opus"
    assert destination.mode == "exact_replay"


def test_cc_switch_render_accepts_an_explicit_unset_legacy_source(
    main_db,
    tmp_path,
):
    from subagents import cc_runtime

    main_db.execute("UPDATE threads SET model=NULL WHERE id='main-1'")
    main_db.commit()
    prepared = _prepared_context(_projection(), mode="exact")

    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="",
        selection=prepared.replay,
        provider_resume=False,
        expected_selected_model="",
        is_subagent=False,
        db=main_db,
    )

    assert destination.target_catalog_id == "claude-code/opus"
    assert destination.mode == "exact_replay"


@pytest.mark.asyncio
async def test_cold_cc_checkpoint_reconstruction_uses_tail_and_seed_checkpoint(
    main_db,
    fake_claude,
    tmp_path,
):
    from history.types import HistoryBoundary
    from subagents import cc_runtime

    _seed_decoy_history(main_db)
    checkpoint_boundary = HistoryBoundary(
        thread_id="main-1",
        history_epoch=0,
        seq=2,
        message_id="checkpoint-boundary",
    )
    checkpoint_json = json.dumps(
        {
            "schema_version": 1,
            "objective": "PORTABLE_CHECKPOINT_OBJECTIVE",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    prepared = _prepared_context(
        _projection(
            ("tail-user", 3, "user", "PREPARED_TAIL_USER"),
            ("tail-answer", 4, "assistant", "PREPARED_TAIL_ANSWER"),
            start_exclusive=checkpoint_boundary,
        ),
        mode="checkpoint_tail",
        checkpoint_json=checkpoint_json,
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="current user turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)

    await _feed_main(
        "current user turn",
        prepared_context=prepared,
        binary=fake_claude,
        claude_dir=str(tmp_path / "cc-home"),
    )

    live = cc_runtime._LIVE.get("main-1")
    from registry.models import find_for_thread

    registration = find_for_thread(
        "main-1", "claude-code", "session", db=main_db,
    )
    assert json.loads(registration["metadata"])["checkpoint_generation_id"] == (
        "checkpoint-generation"
    )
    transcript = open(
        live.session.reconstructed_transcript_path,
        encoding="utf-8",
    ).read()
    seed = open(live.session.seed_file, encoding="utf-8").read()
    assert "PREPARED_TAIL_USER" in transcript
    assert "PREPARED_TAIL_ANSWER" in transcript
    assert "DB_DECOY_MUST_NOT_REPLACE_PREPARED_REPLAY" not in transcript
    assert "[CORSIA checkpoint]" in seed
    assert "PORTABLE_CHECKPOINT_OBJECTIVE" in seed
    assert "PREPARED_TAIL_USER" not in seed
    frame_history = json.dumps(
        live.context_snapshot["frozen_prompt_material"]["history"],
        sort_keys=True,
    )
    frame_source_ids = {
        source["source_id"] for source in live.context_snapshot["source_inventory"]
    }
    assert "PREPARED_TAIL_USER" in frame_history
    assert "PREPARED_TAIL_ANSWER" in frame_history
    assert "DB_DECOY_MUST_NOT_REPLACE_PREPARED_REPLAY" not in frame_history
    assert "history:portable-checkpoint" in frame_source_ids
    await cc_runtime.retire("main-1")


@pytest.mark.asyncio
async def test_empty_prepared_tail_cold_starts_with_checkpoint_only(
    main_db,
    fake_claude,
    tmp_path,
):
    from history.types import HistoryBoundary
    from subagents import cc_runtime

    _seed_decoy_history(main_db)
    checkpoint_boundary = HistoryBoundary(
        thread_id="main-1",
        history_epoch=0,
        seq=2,
        message_id="checkpoint-boundary",
    )
    prepared = _prepared_context(
        _projection(start_exclusive=checkpoint_boundary),
        mode="checkpoint_tail",
        checkpoint_json=json.dumps(
            {
                "schema_version": 1,
                "objective": "EMPTY_TAIL_CHECKPOINT_OBJECTIVE",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="current user turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)

    await _feed_main(
        "current user turn",
        prepared_context=prepared,
        binary=fake_claude,
        claude_dir=str(tmp_path / "cc-home"),
    )

    live = cc_runtime._LIVE.get("main-1")
    seed = open(live.session.seed_file, encoding="utf-8").read()
    assert live.session.resume_session_id is None
    assert live.session.reconstructed_transcript_path is None
    assert "EMPTY_TAIL_CHECKPOINT_OBJECTIVE" in seed
    assert "DB_DECOY_MUST_NOT_REPLACE_PREPARED_REPLAY" not in seed
    await cc_runtime.retire("main-1")


@pytest.mark.parametrize("mode", ["exact", "checkpoint_tail"])
def test_cold_cc_destination_mints_identity_after_registered_session(
    main_db,
    tmp_path,
    mode,
):
    """A cold launch must not reuse an existing Claude conversation UUID."""
    from history.types import HistoryBoundary
    from subagents import cc_runtime

    prior_session_id = str(uuid.uuid4())
    _register_live_provider(main_db, prior_session_id)
    boundary = HistoryBoundary(
        thread_id="main-1",
        history_epoch=0,
        seq=2,
        message_id="checkpoint-boundary",
    )
    projection = (
        _projection(start_exclusive=boundary)
        if mode == "checkpoint_tail"
        else _projection(
            ("prepared-user", 1, "user", "hello"),
            ("prepared-answer", 2, "assistant", "hi"),
        )
    )
    prepared = _prepared_context(
        projection,
        mode=mode,
        checkpoint_json=(
            json.dumps({"schema_version": 1, "objective": "continue"})
            if mode == "checkpoint_tail"
            else None
        ),
    )

    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="current user turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )

    assert destination.cc_session_id != prior_session_id
    assert destination.prior_cc_session_id == prior_session_id


def test_cold_cc_start_rejects_changed_registration(main_db, tmp_path):
    """A stale cold preflight cannot overwrite a newer provider identity."""
    from registry.listing import find_for_thread
    from registry.service import register_cc_session
    from subagents import cc_runtime
    from subagents.cc_runtime_errors import CCPreparedDestinationStale
    from subagents.cc_startup_context import build_cc_startup_context

    prior_session_id = str(uuid.uuid4())
    current_session_id = str(uuid.uuid4())
    _register_live_provider(main_db, prior_session_id)
    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "hello"),
            ("prepared-answer", 2, "assistant", "hi"),
        ),
        mode="exact",
    )
    target = cc_target()
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=target,
        current_user_text="current user turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)
    register_cc_session("main-1", current_session_id, db=main_db)
    row = main_db.execute(
        "SELECT * FROM threads WHERE id='main-1'"
    ).fetchone()

    with pytest.raises(CCPreparedDestinationStale, match="registration changed"):
        build_cc_startup_context(
            "main-1",
            "current user turn",
            row=row,
            target=target,
            is_subagent=False,
            prepared_context=prepared,
            db=main_db,
        )

    current = find_for_thread(
        "main-1", "claude-code", "session", db=main_db
    )
    assert current is not None
    assert current["external_id"] == current_session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn_first", [False, True])
async def test_rejected_resume_repair_requires_fresh_preflight(main_db, spawn_first):
    from subagents import cc_runtime
    from subagents.cc_feed_transition import CCFeedTransition

    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "prepared"),
            ("prepared-answer", 2, "assistant", "answer"),
        ),
        mode="exact",
    )
    rejected = SimpleNamespace(
        synthetic_resume=False,
        resume_rejected=True,
        last_exit_stderr="rejected",
        session=SimpleNamespace(resume_session_id="missing-cc"),
    )
    rebuilt = (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    replace = AsyncMock(return_value=rebuilt)
    retire = Mock(return_value=True)
    initial = _prepared_provider_resume(_projection())
    refresh = AsyncMock(side_effect=[initial, prepared] if spawn_first else [prepared])
    cc_runtime._LIVE.reset()
    transition = CCFeedTransition(
        thread_id="main-1", text="continue", target=cc_target(), effort=None,
        claude_dir=None, binary=None, broadcast=None, is_subagent=False,
        bcast=lambda _thread_id, _payload: None, db=main_db,
        prepared_context=initial, refresh_callback=refresh,
        held_lock=cc_runtime._LIVE.lock("main-1"),
    )
    cc_runtime._LIVE.put("main-1", rejected)

    with (
        patch(
            "subagents.cc_feed_transition.replace_same_turn_after_checkpoint_locked",
            replace,
        ),
        patch("subagents.cc_feed_transition.retire_rejected_provider_session", retire),
        patch("subagents.cc_feed_transition.require_cc_provider_fit", lambda *_: None),
    ):
        async with transition.held_lock:
            if spawn_first:
                with patch(
                    "subagents.cc_feed_transition.resume_child",
                    AsyncMock(return_value=rejected),
                ):
                    assert await transition.resume() is rejected
                assert transition.prepared_context is initial
            result = await transition.rebuild_rejected_resume(rejected)

    assert result is rebuilt
    retire.assert_called_once_with("main-1", "missing-cc", None)
    assert refresh.await_count == (2 if spawn_first else 1)
    assert transition.prepared_context is prepared
    assert replace.await_args.args[:4] == ("main-1", rejected, prepared, "continue")
    assert replace.await_args.kwargs["held_lock"] is transition.held_lock
    cc_runtime._LIVE.reset()


def test_provider_resume_frame_does_not_replay_or_install_checkpoint(main_db):
    from subagents import cc_destination, cc_runtime
    from subagents.cc_runtime_state import _Live

    provider_session_id = str(uuid.uuid4())
    working_dir = _register_live_provider(main_db, provider_session_id)
    live = _Live(
        session=SimpleNamespace(
            working_dir=working_dir,
            thread_id=provider_session_id,
            resume_session_id=provider_session_id,
            reconstructed_transcript_path=None,
            returncode=None,
            pid=101,
        ),
        parent_thread_id=None,
        name="cc",
        model="opus",
        catalog_model_id="claude-code/opus",
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", live)
    prepared = _prepared_provider_resume(
        _projection(
            ("prepared-user", 1, "user", "provider-owned history"),
            ("prepared-answer", 2, "assistant", "provider-owned answer"),
        )
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="continue live",
        selection=None,
        provider_resume=True,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)

    with patch(
        "history.projection.render_portable_history",
        side_effect=AssertionError("live handle must not replay CORSIA history"),
    ):
        result = cc_destination.build_cc_user_frame(
            thread_id="main-1",
            text="continue live",
            live=live,
            target=cc_target(),
            prepared_context=prepared,
            db=main_db,
        )

    assert result is destination.frame_reconstruction
    material = result.snapshot.frozen_prompt_material
    assert material["history"] == []
    assert material["provider_payload"]["history_source_attribution"] == (
        "provider_handle_opaque"
    )
    assert destination.source_tokens == 0
    assert destination.source_section_tokens["tools"] == 0
    assert destination.fit_section_tokens["tools"] == 0
    assert destination.input_tokens == destination.source_section_tokens[
        "current_user_frame"
    ]
    assert "checkpoint_text" not in material["provider_payload"]
    cc_runtime._LIVE.reset()


def test_cc_destination_adapter_freezes_cold_provider_payload(main_db, tmp_path):
    from subagents import cc_runtime

    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "FROZEN_EXACT_USER"),
            ("prepared-answer", 2, "assistant", "FROZEN_EXACT_ANSWER"),
        ),
        mode="exact",
    )

    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="frozen current turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )

    assert destination.mode == "exact_replay"
    assert destination.live_handle_fingerprint is None
    assert destination.seed_reconstruction is not None
    assert destination.frame_reconstruction.turn_text.startswith("frozen current turn")
    assert destination.transcript is not None
    assert "FROZEN_EXACT_USER" in destination.transcript.content
    assert "FROZEN_EXACT_ANSWER" in destination.transcript.content
    assert destination.input_tokens > 0
    assert destination.source_section_tokens["history"] > 0


@pytest.mark.asyncio
async def test_cc_runtime_consumes_frozen_destination_without_rerender(
    main_db,
    fake_claude,
    tmp_path,
):
    from subagents import cc_runtime

    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "FROZEN_RUNTIME_USER"),
            ("prepared-answer", 2, "assistant", "FROZEN_RUNTIME_ANSWER"),
        ),
        mode="exact",
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="frozen runtime turn",
        selection=prepared.replay,
        provider_resume=False,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)

    with (
        patch(
            "subagents.cc_context_reconstruction.build_cc_context_reconstruction",
            side_effect=AssertionError("runtime must consume the frozen seed"),
        ),
        patch(
            "subagents.cc_context_reconstruction.build_cc_user_frame_reconstruction",
            side_effect=AssertionError("runtime must consume the frozen frame"),
        ),
        patch(
            "history.projection.render_portable_history",
            side_effect=AssertionError("runtime must not reread prepared history"),
        ),
        patch(
            "subagents.cc_context_reconstruction.reconstruct_cc_transcript_from_corsia",
            side_effect=AssertionError("runtime must install the frozen transcript"),
        ),
    ):
        await _feed_main(
            "frozen runtime turn",
            prepared_context=prepared,
            binary=fake_claude,
            claude_dir=str(tmp_path / "cc-home"),
        )

    live = cc_runtime._LIVE.get("main-1")
    assert open(live.session.seed_file, encoding="utf-8").read() == (
        destination.seed_reconstruction.seed_text
    )
    assert (
        open(
            live.session.reconstructed_transcript_path,
            encoding="utf-8",
        ).read()
        == destination.transcript.content
    )
    assert live.session.input_frame_log[-1]["message"]["content"] == (
        destination.frame_reconstruction.turn_text
    )
    await cc_runtime.retire("main-1")


@pytest.mark.asyncio
async def test_cold_runtime_uses_frozen_tool_registry_and_definitions(
    main_db,
    fake_claude,
    tmp_path,
):
    from api import cc_attach as cc_attach_api
    from starlette.requests import Request
    from subagents import cc_runtime

    sentinel_registry = SimpleNamespace(version="prepared")
    sentinel_definitions = (
        {
            "type": "function",
            "function": {
                "name": "sentinel_tool",
                "description": "frozen provider definition",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    )
    prepared = _prepared_context(
        _projection(
            ("prepared-user", 1, "user", "tool registry history"),
            ("prepared-answer", 2, "assistant", "tool registry answer"),
        ),
        mode="exact",
    )
    with (
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            return_value=sentinel_registry,
        ),
        patch(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            return_value=sentinel_definitions,
        ),
    ):
        destination = cc_runtime.prepare_cc_destination_payload(
            "main-1",
            target=cc_target(),
            current_user_text="use frozen tools",
            selection=prepared.replay,
            provider_resume=False,
            is_subagent=False,
            db=main_db,
        )
    prepared = _with_rendered_destination(prepared, destination)

    assert destination.tool_registry is sentinel_registry
    assert destination.tool_definitions == sentinel_definitions

    def _configure_frozen_bridge(live, **kwargs):
        live.tool_registry = kwargs["registry"]
        live.tool_token = "prepared-tool-token"
        live.tool_config = "prepared-tool-config"
        return live.tool_config

    configure = patch(
        "subagents.harness_tool_bridge.configure_cc_tool_bridge",
        side_effect=_configure_frozen_bridge,
    )
    with (
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            side_effect=AssertionError("cold runtime rebuilt the frozen registry"),
        ),
        configure as configure_bridge,
    ):
        await _feed_main(
            "use frozen tools",
            prepared_context=prepared,
            binary=fake_claude,
            claude_dir=str(tmp_path / "cc-home"),
        )

    assert configure_bridge.call_args.kwargs["registry"] is sentinel_registry
    live = cc_runtime._LIVE.get("main-1")
    assert live is not None
    assert live.tool_definitions == sentinel_definitions
    assert live.prepared_destination_identity is destination.launch_identity

    # Neither a later registry mutation nor changed launch configuration may
    # alter the provider schema that preflight measured and approved.
    sentinel_registry.version = "mutated-after-preflight"
    live.tool_config = "mutated-after-preflight"
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 9000),
            "headers": [],
        }
    )
    with patch.object(
        cc_attach_api,
        "dynamic_tool_definitions",
        side_effect=AssertionError("tool schemas were re-derived after preflight"),
        create=True,
    ):
        listed = await cc_attach_api.list_cc_tools(
            request,
            "main-1",
            token=live.tool_token,
        )
    assert listed == {"tools": list(sentinel_definitions)}
    await cc_runtime.retire("main-1")


def test_provider_resume_fingerprint_rejects_replaced_guard(main_db):
    from subagents import cc_destination, cc_runtime
    from subagents.cc_runtime_state import _Live

    provider_session_id = str(uuid.uuid4())
    working_dir = _register_live_provider(main_db, provider_session_id)

    def _live(pid: int):
        return _Live(
            session=SimpleNamespace(
                working_dir=working_dir,
                thread_id=provider_session_id,
                resume_session_id=provider_session_id,
                reconstructed_transcript_path=None,
                returncode=None,
                pid=pid,
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
        )

    original = _live(101)
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", original)
    prepared = _prepared_provider_resume(
        _projection(
            ("prepared-user", 1, "user", "provider-owned history"),
            ("prepared-answer", 2, "assistant", "provider-owned answer"),
        )
    )
    destination = cc_runtime.prepare_cc_destination_payload(
        "main-1",
        target=cc_target(),
        current_user_text="continue live",
        selection=None,
        provider_resume=True,
        is_subagent=False,
        db=main_db,
    )
    prepared = _with_rendered_destination(prepared, destination)
    missing_epoch = SimpleNamespace(
        **{
            name: value
            for name, value in vars(prepared).items()
            if name != "runtime_context_epoch"
        }
    )
    with pytest.raises(AttributeError, match="runtime_context_epoch"):
        cc_destination.build_cc_user_frame(
            thread_id="main-1",
            text="continue live",
            live=original,
            target=cc_target(),
            prepared_context=missing_epoch,
            db=main_db,
        )
    replacement = _live(202)
    cc_runtime._LIVE.put("main-1", replacement)

    with pytest.raises(
        cc_runtime.CCPreparedDestinationStale,
        match="live handle changed",
    ):
        cc_destination.build_cc_user_frame(
            thread_id="main-1",
            text="continue live",
            live=replacement,
            target=cc_target(),
            prepared_context=prepared,
            db=main_db,
        )
    cc_runtime._LIVE.reset()


def test_conductor_designation_invalidates_live_destination_before_render(main_db):
    from infra.conductor import set_scope_coordinator
    from subagents import cc_runtime
    from subagents.cc_runtime_state import _Live

    provider_session_id = str(uuid.uuid4())
    working_dir = _register_live_provider(main_db, provider_session_id)
    live = _Live(
        session=SimpleNamespace(
            working_dir=working_dir,
            thread_id=provider_session_id,
            resume_session_id=provider_session_id,
            reconstructed_transcript_path=None,
            returncode=None,
            pid=101,
        ),
        parent_thread_id=None,
        name="cc",
        model="opus",
        catalog_model_id="claude-code/opus",
        runtime_context_epoch=0,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", live)

    assert set_corsia_agent("main-1", db=main_db)

    with pytest.raises(
        cc_runtime.CCPreparedDestinationStale,
        match="older runtime context epoch",
    ):
        cc_runtime.prepare_cc_destination_payload(
            "main-1",
            target=cc_target(),
            current_user_text="continue after designation",
            selection=None,
            provider_resume=True,
            is_subagent=False,
            db=main_db,
        )
    cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_safe_boundary_refresh_precedes_cold_start_frame_and_provider_write():
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime
    from subagents.cc_runtime_state import _Live

    initial = SimpleNamespace(name="initial", runtime_context_epoch=0)
    refreshed = _proved_sequence_context(name="refreshed", runtime_context_epoch=0)
    events: list[tuple[str, object | None]] = []
    session = SimpleNamespace(returncode=None)
    live = _Live(
        session=session,
        parent_thread_id=None,
        name="cc",
        model="opus",
    )

    async def refresh():
        events.append(("refresh", None))
        return refreshed

    async def resume(*_args, **kwargs):
        events.append(("resume", kwargs["prepared_context"]))
        cc_runtime._LIVE.put("main-1", live)
        return live

    def build(**kwargs):
        events.append(("frame", kwargs["prepared_context"]))
        return SimpleNamespace()

    async def persist():
        events.append(("persist", None))

    async def accept(*_args, **_kwargs):
        events.append(("provider", None))
        return TurnIdentity("prepared-turn", 1)

    cc_runtime._LIVE.reset()
    with patch("subagents.cc_feed_transition.resume_child", new=resume):
        with patch("subagents.cc_feed.build_cc_user_frame", new=build):
            with patch(
                "subagents.cc_feed.accept_reconstructed_input",
                new=accept,
            ):
                result = await cc_runtime.feed_main(
                    "main-1",
                    "continue",
                    target=cc_target(),
                    turn_id="prepared-turn",
                    prepared_context=initial,
                    refresh_prepared_context=refresh,
                    persist_input_once=persist,
                )

    assert result == TurnIdentity("prepared-turn", 1)
    assert events == [
        ("refresh", None),
        ("resume", refreshed),
        ("frame", refreshed),
        ("persist", None),
        ("provider", None),
    ]
    cc_runtime._LIVE.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exact_replay", "checkpoint_tail"])
@pytest.mark.parametrize(
    "predecessor_active",
    [False, True],
    ids=["idle-overflow", "post-interrupt-retry"],
)
async def test_live_refresh_to_cold_retires_exact_handle_before_provider_write(
    mode,
    predecessor_active,
):
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime
    from subagents.cc_runtime_state import _Live

    launch_identity = object()
    initial = SimpleNamespace(mode="provider_resume", runtime_context_epoch=0)
    refreshed = _proved_sequence_context(mode=mode, runtime_context_epoch=0)
    destination = SimpleNamespace(
        mode=mode,
        launch_identity=launch_identity,
    )
    events: list[tuple[str, object | None]] = []
    old = _Live(
        session=SimpleNamespace(returncode=None),
        parent_thread_id=None,
        name="old-live",
        model="opus",
        catalog_model_id="claude-code/opus",
        initialized=True,
        turn_active=predecessor_active,
    )
    replacement = _Live(
        session=SimpleNamespace(returncode=None),
        parent_thread_id=None,
        name="cold-replacement",
        model="opus",
        catalog_model_id="claude-code/opus",
        initialized=True,
    )
    replacement.prepared_destination_identity = launch_identity

    async def refresh():
        events.append(("refresh", None))
        return refreshed

    async def preempt(_thread_id, live, **_kwargs):
        assert live is old
        events.append(("interrupt-terminal", live))
        live.turn_active = False
        return live

    async def close(thread_id, live, *_args):
        lock = cc_runtime._LIVE.existing_lock(thread_id)
        assert lock is not None and lock.locked()
        assert live is old
        events.append(("close", live))
        assert cc_runtime._LIVE.pop(thread_id) is old
        return True

    async def resume(thread_id, **kwargs):
        assert cc_runtime._LIVE.get(thread_id) is None
        assert kwargs["prepared_context"] is refreshed
        events.append(("resume", kwargs["prepared_context"]))
        cc_runtime._LIVE.put(thread_id, replacement)
        return replacement

    def prepared_destination(*_args, prepared_context, **_kwargs):
        assert prepared_context is refreshed
        return destination

    def build(**kwargs):
        assert kwargs["live"] is replacement
        assert kwargs["prepared_context"] is refreshed
        assert replacement.user_driving is True
        assert replacement.catalog_model_id == "claude-code/opus"
        events.append(("frame", kwargs["prepared_context"]))
        return SimpleNamespace()

    async def persist():
        events.append(("persist", None))

    async def accept(*_args, **_kwargs):
        events.append(("provider", None))
        return TurnIdentity("prepared-turn", 1)

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", old)
    if predecessor_active:
        with pytest.raises(cc_runtime.CCLiveTurnBusy):
            await cc_runtime.feed_main(
                "main-1",
                "overflow successor",
                target=cc_target(),
                turn_id="prepared-turn",
                prepared_context=initial,
                refresh_prepared_context=refresh,
                persist_input_once=persist,
            )
        assert events == []
        assert cc_runtime._LIVE.get("main-1") is old
    else:
        with (
            patch("subagents.cc_runtime._preempt_active_turn", new=preempt),
            patch("subagents.cc_feed_transition.close_and_finalize", new=close),
            patch("subagents.cc_feed_transition.resume_child", new=resume),
            patch(
                "subagents.cc_feed_transition.prepared_destination_from_context",
                new=prepared_destination,
            ),
            patch("subagents.cc_feed.build_cc_user_frame", new=build),
            patch(
                "subagents.cc_feed.accept_reconstructed_input",
                new=accept,
            ),
        ):
            result = await cc_runtime.feed_main(
                "main-1",
                "overflow successor",
                target=cc_target(),
                turn_id="prepared-turn",
                prepared_context=initial,
                refresh_prepared_context=refresh,
                persist_input_once=persist,
            )

        assert result == TurnIdentity("prepared-turn", 1)
        assert events == [
            ("refresh", None),
            ("close", old),
            ("resume", refreshed),
            ("frame", refreshed),
            ("persist", None),
            ("provider", None),
        ]
    cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_hard_preempt_refreshes_after_settlement_before_replacement():
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from subagents import cc_runtime
    from subagents.cc_runtime_state import _Live

    initial = SimpleNamespace(name="initial")
    refreshed = SimpleNamespace(name="refreshed")
    events: list[str] = []
    session = SimpleNamespace(
        interrupt=AsyncMock(return_value=False),
        terminate=AsyncMock(),
        pid=77,
    )
    live = _Live(
        session=session,
        parent_thread_id=None,
        name="cc",
        model="opus",
        corsia_turn=TurnIdentity("predecessor-turn", 1),
        turn_active=True,
    )
    replacement = SimpleNamespace(name="replacement")

    async def refresh():
        events.append("refresh")
        return refreshed

    async def settle(**_kwargs):
        events.append("settle")

    async def commit(*_args, **_kwargs):
        events.append("terminal")
        return TurnTerminalResult.APPLIED

    async def replace(*_args, **kwargs):
        events.append("replace")
        assert kwargs["prepared_context"] is refreshed
        return replacement

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("main-1", live)
    with (
        patch(
            "subagents.cc_preempt.replay_pending_exit_before_preempt",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "subagents.cc_preempt.prepare_live_close",
            new=AsyncMock(return_value=(10_000.0, set())),
        ),
        patch(
            "subagents.cc_preempt.join_admitted_tool_tasks",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "subagents.cc_preempt.settle_missing_tool_results",
            new=settle,
        ),
        patch(
            "subagents.cc_preempt.commit_interrupted_terminal",
            new=commit,
        ),
        patch(
            "subagents.cc_preempt.replace_preempted_process",
            new=replace,
        ),
    ):
        result = await cc_runtime._preempt_active_turn(
            "main-1",
            live,
            target=cc_target(),
            effort=None,
            claude_dir=None,
            binary=None,
            broadcast=None,
            is_subagent=False,
            text="successor",
            bcast=lambda _thread_id, _payload: None,
            prepared_context=initial,
            refresh_prepared_context=refresh,
            held_lock=cc_runtime._LIVE.lock("main-1"),
        )

    assert result is replacement
    assert events == ["settle", "terminal", "refresh", "replace"]
    cc_runtime._LIVE.reset()
