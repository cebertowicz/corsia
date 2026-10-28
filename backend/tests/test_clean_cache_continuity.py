"""Cache-continuity contracts shared by live send, restart, and prewarm."""

from __future__ import annotations

from types import SimpleNamespace
import json

import pytest


@pytest.mark.parametrize(
    ("harness", "register", "integration", "artifact_type"),
    (
        ("claude-code", "register_cc_session", "claude-code", "session"),
        ("codex", "register_codex_thread", "codex", "thread"),
    ),
)
def test_provider_registration_persists_checkpoint_identity_across_restart(
    isolated_db, harness, register, integration, artifact_type,
) -> None:
    """A reconstructed continuation's checkpoint identity is durable."""
    from infra.database import get_db
    from registry import service
    from registry.models import find_for_thread

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-checkpoint','user-1',?,'t0','t0')",
        (f"{harness}/test",),
    )
    db.commit()
    getattr(service, register)(
        "thread-checkpoint",
        "provider-continuation",
        checkpoint_generation_id="checkpoint-7",
        db=db,
    )
    getattr(service, register)(
        "thread-checkpoint", "provider-continuation", db=db,
    )
    db.commit()
    db.close()

    restarted = get_db()
    row = find_for_thread(
        "thread-checkpoint", integration, artifact_type, db=restarted,
    )
    restarted.close()

    assert json.loads(row["metadata"])["checkpoint_generation_id"] == "checkpoint-7"


@pytest.mark.parametrize(
    ("harness", "model", "trigger", "source_model", "expected"),
    (
        ("claude-code", "claude-code/sonnet", "pressure", None, False),
        ("codex", "codex/gpt-5-codex", "pressure", None, False),
        ("claude-code", "claude-code/sonnet", "model_switch", None, True),
        ("codex", "codex/gpt-5-codex", "pressure", "codex/old", True),
    ),
)
def test_stale_registration_cannot_resume_over_eligible_checkpoint(
    isolated_db, harness, model, trigger, source_model, expected,
) -> None:
    """Only a matching manual/pressure install invalidates old continuation."""
    from infra import runtime_truth
    from infra.database import get_db
    from registry.service import register_cc_session, register_codex_thread

    thread_id = f"thread-{harness}-{trigger}-{source_model or 'current'}"
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,'user-1',?,'t0','t0')",
        (thread_id, model),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES ('m1',?,'user','body','t0',1)",
        (thread_id,),
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES ('checkpoint-new',?,0,1,'m1',?,'warm','installed',?,?,?,?,0,'[]','t0')",
        (thread_id, trigger, harness, source_model or model, harness, model),
    )
    if harness == "claude-code":
        register_cc_session(
            thread_id, "provider-old", checkpoint_generation_id="checkpoint-old", db=db,
        )
        check = runtime_truth._has_registered_cc_session
    else:
        register_codex_thread(
            thread_id, "provider-old", checkpoint_generation_id="checkpoint-old", db=db,
        )
        check = runtime_truth._has_registered_codex_thread
    db.commit()
    db.close()

    assert check(thread_id) is expected


def test_provider_boundary_snapshot_keeps_raw_rescue_separate_from_eager_adoption(
    isolated_db,
) -> None:
    """One snapshot retains portable rescue while narrowing eager adoption."""
    from infra.database import get_db
    from infra.runtime_truth import provider_boundary_snapshot

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/current','t0','t0')"
    )
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES (?,'thread-1','user','body','t0',?)",
        (("m1", 1), ("m2", 2)),
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES ('installed-switch','thread-1',0,1,'m1','model_switch','cold',"
        "'installed','claude-code','claude-code/old','codex','codex/current',"
        "0,'[]','t0')"
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES ('active-old-model','thread-1',0,2,'m2','pressure','warm',"
        "'running','codex','codex/old','codex','codex/old',0,'[]','t1')"
    )
    db.commit()

    active, installed, eager = provider_boundary_snapshot(
        "thread-1", "codex", "codex/current", db=db,
    )
    db.close()

    assert active.id == "active-old-model"
    assert installed.id == "installed-switch"
    assert eager is None


@pytest.mark.asyncio
async def test_cc_durable_session_survives_process_eviction(
    isolated_db, monkeypatch,
) -> None:
    """A missing local CC process never authorizes transcript replay."""
    from infra.database import get_db
    from infra.runtime_truth import read_runtime_truth
    from llm.resolution import resolve_execution_target
    from registry.service import register_cc_session
    from subagents import cc_runtime

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t0','t0')"
    )
    db.commit()
    register_cc_session("thread-1", "provider-session-1", db=db)
    db.commit()
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()
    monkeypatch.setattr(cc_runtime, "is_live", lambda _thread_id: False)

    truth = await read_runtime_truth(
        "thread-1",
        thread_row,
        target=resolve_execution_target("claude-code/sonnet"),
    )

    assert truth["cc_live"] is False
    assert truth["cc_has_session"] is True


@pytest.mark.asyncio
async def test_live_cc_process_does_not_invent_a_durable_session(
    isolated_db, monkeypatch,
) -> None:
    """Live process status and restart-resumable identity remain distinct."""
    from infra.runtime_truth import _cc_runtime_truth
    from subagents import cc_runtime

    monkeypatch.setattr(cc_runtime, "is_live", lambda _thread_id: True)

    assert await _cc_runtime_truth("thread-1", True) == (True, False)


@pytest.mark.asyncio
async def test_live_codex_process_still_reports_its_durable_registration(
    isolated_db, monkeypatch,
) -> None:
    """A live turn cannot hide whether restart can resume its provider thread."""
    from infra.database import get_db
    from infra.runtime_truth import _codex_runtime_truth
    from registry.service import register_codex_thread
    from subagents import codex_runtime

    db = get_db()
    register_codex_thread("thread-1", "provider-thread-1", db=db)
    db.commit()
    db.close()
    monkeypatch.setattr(codex_runtime, "is_live", lambda _thread_id: True)

    live, _untrusted, durable, _prior = await _codex_runtime_truth(
        "thread-1", True,
    )
    assert live is True
    assert durable is True


@pytest.mark.asyncio
async def test_in_memory_codex_handle_does_not_invent_durable_registration(
    isolated_db, monkeypatch,
) -> None:
    """Retiring the DB row prevents restart from restoring an old handle."""
    from infra.runtime_truth import _codex_runtime_truth
    from subagents import codex_runtime

    monkeypatch.setattr(codex_runtime, "is_live", lambda _thread_id: False)

    async def in_memory_handle(_thread_id: str) -> bool:
        return True

    monkeypatch.setattr(codex_runtime, "has_thread_id", in_memory_handle)

    _live, _untrusted, durable, _prior = await _codex_runtime_truth(
        "thread-1", True,
    )
    assert durable is False


def test_cc_eviction_prepares_the_same_provider_resume(
    isolated_db, monkeypatch, tmp_path,
) -> None:
    """Preflight after process eviction sends only the new frame to one session."""
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from registry.service import register_cc_session
    from subagents.cc_destination import prepare_cc_destination_payload
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1','claude-code/sonnet','t0','t0')"
        )
        db.commit()
        register_cc_session(
            "thread-1", "provider-session-1", cwd=str(tmp_path), db=db,
        )
        db.commit()
        target = resolve_execution_target("claude-code/sonnet", db=db)
    finally:
        db.close()
    _LIVE.pop("thread-1", None)
    # The evicted session's transcript stays in the root its resume launches in.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    transcript = tmp_path / ".claude" / "projects" / "-p" / "provider-session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    seed = SimpleNamespace(seed_text="opaque provider seed")
    frame = SimpleNamespace(turn_text="next frame")
    monkeypatch.setattr(
        "subagents.cc_destination.build_cc_context_reconstruction",
        lambda **_kwargs: seed,
    )
    monkeypatch.setattr(
        "subagents.cc_destination.build_cc_user_frame_reconstruction",
        lambda **_kwargs: frame,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry",
        lambda _cwd: object(),
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry: (),
    )

    first = prepare_cc_destination_payload(
        "thread-1", target=target, current_user_text="continue",
        selection=None, provider_resume=True,
    )
    _LIVE.pop("thread-1", None)
    second = prepare_cc_destination_payload(
        "thread-1", target=target, current_user_text="continue",
        selection=None, provider_resume=True,
    )

    assert first.mode == second.mode == "provider_resume"
    assert first.cc_session_id == second.cc_session_id == "provider-session-1"
    assert first.history == second.history == ()
    assert first.transcript is second.transcript is None


@pytest.mark.asyncio
async def test_explicit_history_boundary_retires_cc_provider_session(
    isolated_db,
) -> None:
    """History editing invalidates the provider transcript; eviction does not."""
    from infra.database import get_db
    from registry.models import find_for_thread
    from registry.service import register_cc_session
    from subagents import cc_runtime

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t0','t0')"
    )
    db.commit()
    register_cc_session("thread-1", "provider-session-1", db=db)
    db.commit()
    db.close()

    assert await cc_runtime.retire("thread-1") is True
    db = get_db()
    try:
        assert find_for_thread(
            "thread-1", "claude-code", "session", db=db,
        ) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_preflight_counts_exact_history_without_replacing_provider_resume(
    monkeypatch,
) -> None:
    """Missing usage renders a count, then sends once through the same session."""
    import infra.send_preflight as preflight

    candidates = SimpleNamespace(
        exact=object(), checkpoint_tail=object(), last_resort=object(),
        sealed_boundary=object(), history_epoch=0,
    )
    calls: list[str] = []

    class Result:
        def __init__(self, status: str):
            self.status = status

        def to_dict(self):
            return {"status": self.status, "continuity_mode": "provider_resume"}

    class Attempt:
        thread_id = "thread-1"
        provider = "cc"
        target = SimpleNamespace(catalog_id="claude-code/sonnet")

        async def live(self, *_args, **_kwargs):
            calls.append("provider_resume")
            return Result("blocked" if len(calls) == 1 else "allowed")

        async def cold(self, *_args, **_kwargs):
            calls.append("replay")
            return Result("allowed")

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(preflight, "_read_candidates", lambda *_args: candidates)
    from infra.provider_resume import ProviderResumeDecision

    monkeypatch.setattr(
        preflight, "provider_resume_decision",
        lambda *_args, **_kwargs: ProviderResumeDecision(True, None),
    )

    target = SimpleNamespace(catalog_id="claude-code/sonnet")

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": target.catalog_id},
        provider="cc",
        target=target,
        runtime_truth={"cc_has_session": True},
        current_user_content="hello",
    )

    assert result["continuity_mode"] == "provider_resume"
    assert calls == ["replay", "provider_resume"]


@pytest.mark.asyncio
async def test_codex_distrust_preserves_durable_provider_thread() -> None:
    """Local-process distrust cannot erase the resumable Codex identity."""
    from subagents import codex_runtime
    from subagents.codex_runtime_state import (
        _CODEX_THREADS,
        _PROVIDER_CONTEXT_UNTRUSTED,
        mark_provider_context_untrusted,
    )

    _CODEX_THREADS["thread-1"] = "provider-thread-1"
    try:
        mark_provider_context_untrusted("thread-1")
        assert _CODEX_THREADS["thread-1"] == "provider-thread-1"
        assert await codex_runtime.has_thread_id("thread-1") is True
    finally:
        _CODEX_THREADS.pop("thread-1", None)
        _PROVIDER_CONTEXT_UNTRUSTED.discard("thread-1")


def test_codex_thread_only_resume_survives_preflight_consumption() -> None:
    """Backend restart must resume the durable thread before spawning locally."""
    from subagents.codex_destination import (
        PreparedCodexDestination,
        prepared_codex_destination,
    )
    from subagents.codex_runtime_state import (
        _APP_SESSIONS,
        _CODEX_THREADS,
        _LIVE,
        current_handle_fingerprint,
    )

    _LIVE.pop("thread-1", None)
    _APP_SESSIONS.pop("thread-1", None)
    _CODEX_THREADS["thread-1"] = "provider-thread-1"
    try:
        destination = PreparedCodexDestination(
            thread_id="thread-1",
            target_catalog_id="codex/test",
            current_user_text="hello",
            delivery_mode="provider_resume",
            provider_thread_id="provider-thread-1",
            handle_fingerprint=current_handle_fingerprint("thread-1"),
            reconstruction=SimpleNamespace(),
            provider_text="hello",
            history_items=(),
            tool_registry=object(),
            dynamic_tools=(),
            input_tokens=1,
            source_tokens=0,
            source_section_tokens=(),
            fit_section_tokens=(),
        )
        prepared = SimpleNamespace(
            target_catalog_id="codex/test",
            mode="provider_resume",
            runtime_context_epoch=0,
            rendered=SimpleNamespace(payload=destination),
        )

        consumed = prepared_codex_destination(
            "thread-1",
            target_catalog_id="codex/test",
            current_user_text="hello",
            prepared_context=prepared,
        )

        assert consumed.provider_thread_id == "provider-thread-1"
    finally:
        _CODEX_THREADS.pop("thread-1", None)


@pytest.mark.asyncio
async def test_codex_restart_restores_before_preparing_current_turn(
    isolated_db, monkeypatch, tmp_path,
) -> None:
    """A persisted Codex thread resumes after restart without transcript replay."""
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from registry.service import register_codex_thread
    from subagents import codex_runtime
    from subagents.codex_destination_render import prepare_codex_destination_payload
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1','codex/gpt-5-codex','t0','t0')"
        )
        assert register_codex_thread(
            "thread-1", "provider-thread-1", db=db,
        ) is not None
        db.commit()
        target = resolve_execution_target("codex/gpt-5-codex", db=db)
    finally:
        db.close()
    _LIVE.pop("thread-1", None)
    _APP_SESSIONS.pop("thread-1", None)
    _CODEX_THREADS.pop("thread-1", None)
    assert await codex_runtime.has_thread_id("thread-1") is True

    reconstruction = SimpleNamespace(
        task_text="must not replay",
        turn_text="current turn only",
        history_items=(),
        snapshot=SimpleNamespace(source_inventory=()),
    )
    registry = object()
    monkeypatch.setattr(
        "subagents.codex_destination_render.codex_destination_scope",
        lambda *_args, **_kwargs: (None, str(tmp_path), False),
    )
    monkeypatch.setattr(
        "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
        lambda **_kwargs: reconstruction,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry",
        lambda _cwd: registry,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry, _thread_context=None: (),
    )

    destination = prepare_codex_destination_payload(
        thread_id="thread-1",
        user_text="continue after restart",
        target=target,
        replay_selection=None,
        provider_resume=True,
        history_epoch=0,
        sealed_boundary=None,
        db=object(),
    )

    assert destination.delivery_mode == "provider_resume"
    assert destination.provider_thread_id == "provider-thread-1"
    assert destination.provider_text == "current turn only"
    assert destination.history_items == ()


@pytest.mark.asyncio
async def test_managed_native_turn_requires_prepared_context() -> None:
    """A managed Native Turn cannot independently reconstruct its history."""
    from core.turn_iteration import run_native_iteration

    class ManagedCancel:
        turn_identity = SimpleNamespace(turn_id="turn-1")

    with pytest.raises(RuntimeError, match="requires prepared context"):
        await run_native_iteration(
            SimpleNamespace(),
            "hello",
            "thread-1",
            "web",
            "thread-1",
            lambda *_args: None,
            ManagedCancel(),
            model="native/test",
            turn_provider={},
        )


@pytest.mark.asyncio
async def test_native_prefix_refuses_implicit_history_rebuild() -> None:
    """Native prewarm and send must name the exact replay selection."""
    from core.native_context_prefix import build_real_send_prefix

    with pytest.raises(ValueError, match="explicit replay selection"):
        await build_real_send_prefix(
            agent=SimpleNamespace(),
            thread_id="thread-1",
            turn_model="native/test",
            fresh_context=False,
            project_id=None,
        )


@pytest.mark.asyncio
async def test_turn_preflight_never_uses_retention_holed_last_resort() -> None:
    """No safe replay means checkpoint-or-visible refusal, never truncation."""
    from infra.send_preflight import _installed_if_fitting

    calls: list[object] = []

    class Attempt:
        async def cold(self, selection, *, trigger_reason):
            calls.append((selection, trigger_reason))
            return SimpleNamespace(status="allowed")

    candidates = SimpleNamespace(
        exact=None,
        checkpoint_tail=None,
        last_resort=object(),
    )

    result = await _installed_if_fitting(Attempt(), candidates)

    assert result is None
    assert calls == []
