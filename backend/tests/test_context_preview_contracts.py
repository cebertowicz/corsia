from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _target(*, harness: str = "native", model: str = "native/model"):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=model,
        harness=harness,
        provider=harness,
        backend="network" if harness == "native" else harness,
        transport_model=model.split("/", 1)[-1],
        expected_reported_model=None,
        context_window=32_000,
        available=True,
        availability_reason=None,
        display_name=model,
        supports_reasoning=False,
    )


def _preview_connection() -> tuple[sqlite3.Connection, sqlite3.Row]:
    from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE messages (
            id TEXT, thread_id TEXT, role TEXT, content TEXT, metadata TEXT,
            created_at TEXT, delivery_state TEXT DEFAULT 'delivered', seq INTEGER
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, model TEXT, project_id TEXT, spawned_by TEXT,
            max_tokens INTEGER, history_epoch INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO threads (id,model,project_id,spawned_by,max_tokens) VALUES
            ('preview-thread','native/model',NULL,NULL,512);
        """
    )
    connection.executescript(CHECKPOINT_SCHEMA_SQL)
    thread_row = connection.execute(
        "SELECT * FROM threads WHERE id='preview-thread'"
    ).fetchone()
    return connection, thread_row


def _running_preview_messages():
    return (
        (
            "before-user", "user", "covered history " * 20_000,
            {"turn_id": "turn-before"},
        ),
        (
            "before-answer", "assistant", "covered answer",
            {"turn_id": "turn-before"},
        ),
        ("live-user", "user", "continue", {"turn_id": "turn-live"}),
        (
            "live-step", "assistant", "reading",
            {
                "turn_id": "turn-live",
                "llm_tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }],
            },
        ),
        (
            "live-result", "tool", "small result",
            {
                "turn_id": "turn-live", "assistant_step_id": "live-step",
                "tool_call_id": "call-1", "name": "read",
            },
        ),
    )


def _checkpointed_preview_connection(isolated_db):
    connection = sqlite3.connect(isolated_db)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "INSERT INTO threads "
        "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES ('preview-checkpoint','steve','native/model',0,6,'t','t')"
    )
    connection.executemany(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,'preview-checkpoint',0,'native/model',?, 't', ?, 'boot')",
        (("turn-before", "completed", "t"), ("turn-live", "running", None)),
    )
    connection.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,delivery_state,"
        "active_turn_id,seq) VALUES (?,'preview-checkpoint',?,?,?,'t',"
        "'delivered',?,?)",
        [
            (message_id, role, content, json.dumps(metadata), metadata["turn_id"], seq)
            for seq, (message_id, role, content, metadata)
            in enumerate(_running_preview_messages(), 1)
        ],
    )
    connection.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES ('checkpoint-1','preview-checkpoint',0,2,'before-answer',"
        "'pressure','warm','installed','native','native/model','native',"
        "'native/model',0,'[]','concise checkpoint','[]','t','t','t','t')"
    )
    connection.commit()
    thread_row = connection.execute(
        "SELECT * FROM threads WHERE id='preview-checkpoint'"
    ).fetchone()
    return connection, thread_row


def _patch_preview_sources(monkeypatch, target) -> None:
    import llm.resolution
    from core import context_preview

    monkeypatch.setattr(
        context_preview, "resolve_execution_target", lambda _model, *, db: target
    )
    monkeypatch.setattr(
        llm.resolution, "resolve_execution_target", lambda *_a, **_k: target
    )
    monkeypatch.setattr(
        context_preview,
        "execution_class",
        lambda *_args, **_kwargs: target.harness,
    )
    monkeypatch.setattr(
        context_preview,
        "provider_for",
        lambda _klass: (target.harness, False),
    )
    monkeypatch.setattr(
        context_preview,
        "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=[], agent_facts={}, installed=None, project_cwd=None
        ),
    )


def test_preview_instrumentation_preserves_result(monkeypatch):
    from core import context_preview
    from infra.context_preview_timing import ContextPreviewTiming

    connection, thread_row = _preview_connection()
    target = _target()
    _patch_preview_sources(monkeypatch, target)

    baseline = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
    )
    timing = ContextPreviewTiming("preview-thread")
    timed = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection, timing=timing,
    )

    assert timed == baseline
    assert set(timing.stages) == {
        "preflight", "eligibility", "source_collection", "preview_projection",
        "provider_resume", "preflight.history", "preflight.sources",
        "preflight.checkpoint_tail",
        "preflight.checkpoint_tail.current_installed",
        "preflight.checkpoint_tail.interpreter_yield",
    }


def test_preview_estimate_counts_the_one_collection_it_lists(monkeypatch):
    """The estimate must not collect again under the tokenizer's model (#2830).

    That name never matches an installed boundary, so a second collection
    re-read every static source from disk on each preview.
    """
    from core import context_preview, dynamic_context_registry
    from core.effective_context import ContextSourceEntry

    connection, thread_row = _preview_connection()
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    role_source = ContextSourceEntry(
        source_id="role-context:workspace:workspace:agent:context/roles/agent/AGENT.md",
        origin_section="role",
        label="Agent role context",
        stable_reference="/workspace/context/roles/agent/AGENT.md",
        freshness="fresh",
        inclusion="included",
        reason="selected Agent role",
        owner_surface="document-viewer",
        prompt_content="ROLE FIT PAYLOAD",
        metadata={"provider": "role_context", "role": "agent"},
    )
    selected_models = []

    def collect(**kwargs):
        selected_models.append(kwargs["selected_model"])
        return SimpleNamespace(
            sources=[role_source], agent_facts={}, installed=None, project_cwd=None,
        )

    monkeypatch.setattr(context_preview, "collect_context_sources", collect)
    monkeypatch.setattr(dynamic_context_registry, "collect_context_sources", collect)

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
    )

    assert selected_models == [target.catalog_id]
    assert preview["preflight"]["source_section_tokens"]["role"] > 0


def test_native_preview_estimates_nonzero_and_counts_exact_tools(monkeypatch):
    from dataclasses import replace
    from core import context_preview

    connection, thread_row = _preview_connection()
    # Native builds its prompt against the counted window, so even a known
    # provider capacity is not a budget and must not be reported as one.
    target = replace(_target(), discovered_context_window=128_000)
    _patch_preview_sources(monkeypatch, target)
    definitions = [{
        "type": "function",
        "function": {
            "name": "read_exact",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        tool_definitions=definitions,
    )

    assert preview["total_used"] > 0
    assert preview["token_source"] == "estimated"
    assert preview["token_usage_available"] is False
    assert preview["tool_def_tokens"] > 0
    assert preview["discovered_context_window"] is None
    frozen = preview["context_snapshot"]["frozen_prompt_material"]
    assert frozen["provider_payload"]["tool_definitions"] == definitions
    with pytest.raises(RuntimeError, match="provider changed"):
        context_preview.build_next_turn_context_preview(
            "preview-thread",
            thread_row,
            db=connection,
            tool_definitions=definitions,
            expected_provider="cc",
        )


def test_external_preview_totals_only_rendered_input(monkeypatch):
    """A CC/Codex preview exposes input occupancy, never reply reserve."""
    from core import context_preview

    connection, thread_row = _preview_connection()
    target = _target(harness="codex", model="codex/luna")
    _patch_preview_sources(monkeypatch, target)

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        current_user_content="hello",
    )

    accounted = (
        preview["system_tokens"] + preview["tool_def_tokens"]
        + preview["history_tokens"] + preview["user_tokens"]
    )
    assert preview["total_used"] == accounted


def test_estimated_preview_total_equals_fixed_input_count(monkeypatch):
    from core import context_preview

    connection, thread_row = _preview_connection()
    target = _target()
    _patch_preview_sources(monkeypatch, target)

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection, current_user_content="fixed input",
    )

    assert preview["total_used"] == preview["user_tokens"]
    assert preview["preflight"]["total_tokens"] == preview["user_tokens"]


def test_provider_resume_preview_uses_matching_receipt_without_archive_read(monkeypatch):
    """An eligible handle is estimated from its receipt and tail, not archive rows."""
    from core import context_admission, context_preview
    from infra.provider_resume import ProviderCapacity

    connection, base_thread_row = _preview_connection()
    thread_row = dict(base_thread_row)
    thread_row["model"] = "codex/luna"
    target = _target(harness="codex", model="codex/luna")
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "infra.provider_resume.read_provider_capacity",
        lambda *_args, **_kwargs: ProviderCapacity(
            12_000, 30, "codex", "codex/luna",
        ),
    )
    monkeypatch.setattr(
        context_admission,
        "_delivered_rows",
        lambda *_args, **_kwargs: pytest.fail("provider resume read archive rows"),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        runtime_truth={"codex_has_thread": True},
    )

    assert preview["continuity_mode"] == "provider_resume"
    assert preview["history_tokens"] == 12_030
    assert preview["token_source"] == "estimated"
    connection.close()


def test_provider_resume_decision_rejects_mismatched_receipt_identity(monkeypatch):
    from infra.provider_resume import ProviderCapacity, provider_resume_decision

    monkeypatch.setattr(
        "infra.provider_resume.read_provider_capacity",
        lambda *_args, **_kwargs: ProviderCapacity(
            12_000, 30, "codex", "codex/other",
        ),
    )
    decision = provider_resume_decision(
        "preview-thread",
        provider="codex",
        thread_row={"model": "codex/luna"},
        target=_target(harness="codex", model="codex/luna"),
        runtime_truth={"codex_has_thread": True},
        db=object(),
    )

    assert decision.available is True
    assert decision.capacity is None


def test_external_preview_tool_source_is_not_labeled_native(monkeypatch):
    from core import context_preview

    connection, thread_row = _preview_connection()
    target = _target(harness="codex", model="codex/luna")
    _patch_preview_sources(monkeypatch, target)
    definitions = [{
        "type": "function",
        "function": {
            "name": "read_exact",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        tool_definitions=definitions,
    )

    tool_source = next(
        source for source in preview["context_snapshot"]["source_inventory"]
        if source["origin_section"] == "tools"
    )
    assert tool_source["source_id"] == "codex:tool-definitions"
    assert tool_source["label"] == "Tool definitions"
    assert "Native" not in tool_source["label"]


def test_model_override_projects_selected_harness_before_session_patch(monkeypatch):
    from core import context_preview

    connection, thread_row = _preview_connection()
    target = _target(harness="codex", model="codex/luna")
    _patch_preview_sources(monkeypatch, target)

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        model_override="codex/luna",
    )

    facts = preview["context_snapshot"]["harness_model_facts"]
    assert facts["harness"] == "codex"
    assert facts["model"] == "codex/luna"


def test_preview_uses_installed_checkpoint_and_running_turn_tail(
    isolated_db,
    monkeypatch,
):
    from core import context_preview
    from infra.context_preview_timing import ContextPreviewTiming

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=[], agent_facts={}, installed=None, project_cwd=None
        ),
    )

    baseline = context_preview.build_next_turn_context_preview(
        "preview-checkpoint",
        thread_row,
        db=connection,
    )
    timing = ContextPreviewTiming("preview-checkpoint")
    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint",
        thread_row,
        db=connection,
        timing=timing,
    )

    assert preview == baseline
    assert {
        "preflight.checkpoint_tail.current_installed",
        "preflight.checkpoint_tail.tail_render",
        "preflight.checkpoint_tail.presence_line",
        "preflight.checkpoint_tail.to_native_history",
        "preflight.checkpoint_tail.checkpoint_render",
        "preflight.checkpoint_tail.interpreter_yield",
    } <= timing.stages.keys()
    assert preview["continuity_mode"] == "checkpoint_tail"
    # The checkpoint is a system-prompt component, as Native renders it, so it
    # is counted in system tokens and is not a history message (#1388).
    assert preview["history_messages"] == 3
    assert preview["preflight"]["checkpoint_tokens"] > 0
    assert preview["system_tokens"] >= preview["preflight"]["checkpoint_tokens"]
    assert preview["history_tokens"] < 1_000
    assert preview["total_used"] < preview["model_context_window"]
    connection.close()


def test_preview_reports_checkpoint_ineligible_when_nothing_new_is_sealed(
    isolated_db, monkeypatch,
):
    """The sheet must never offer Checkpoint Now for a click that would be
    refused by the checkpoint-start endpoint's own admission rule (#1952)."""
    from core import context_preview

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(sources=[], agent_facts={}, installed=None, project_cwd=None),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert preview["checkpoint_eligible"] is False
    assert preview["checkpoint_ineligible_reason"] == "no_new_history"
    connection.close()


def test_checkpoint_preview_reuses_the_installed_tail_boundary(monkeypatch, isolated_db):
    from core import checkpoint_eligibility, context_preview
    import history.projection as projection

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    renders = {"checkpoint_tail": 0, "portable": 0}
    original_tail = projection.render_checkpoint_tail
    original_portable = checkpoint_eligibility.render_portable_history

    def checkpoint_tail(*args, **kwargs):
        renders["checkpoint_tail"] += 1
        return original_tail(*args, **kwargs)

    def portable(*args, **kwargs):
        renders["portable"] += 1
        return original_portable(*args, **kwargs)

    monkeypatch.setattr(projection, "render_checkpoint_tail", checkpoint_tail)
    monkeypatch.setattr(checkpoint_eligibility, "render_portable_history", portable)

    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert preview["checkpoint_ineligible_reason"] == "no_new_history"
    assert renders == {"checkpoint_tail": 1, "portable": 0}
    checkpoint_eligibility.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )
    assert renders == {"checkpoint_tail": 1, "portable": 1}
    connection.close()


def test_checkpoint_preview_eligibility_does_not_re_render_history(
    monkeypatch, isolated_db,
):
    from core import checkpoint_eligibility, context_preview

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    _patch_preview_sources(monkeypatch, _target())
    renders = 0
    original = checkpoint_eligibility.render_portable_history

    def portable(*args, **kwargs):
        nonlocal renders
        renders += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(checkpoint_eligibility, "render_portable_history", portable)

    context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert renders == 0
    connection.close()


def test_preview_reports_checkpoint_eligible_once_new_history_is_sealed(
    isolated_db, monkeypatch,
):
    from core import context_preview

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    connection.execute(
        "UPDATE active_turns SET status='completed', ended_at='t' WHERE id='turn-live'"
    )
    connection.commit()
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(sources=[], agent_facts={}, installed=None, project_cwd=None),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert preview["checkpoint_eligible"] is True
    assert preview["checkpoint_ineligible_reason"] is None
    connection.close()


def test_preview_survives_a_history_that_cannot_be_projected(
    isolated_db, monkeypatch,
):
    from core import checkpoint_eligibility, context_preview
    from history.types import PortableHistoryError

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(sources=[], agent_facts={}, installed=None, project_cwd=None),
    )

    def unprojectable(*_args, **_kwargs):
        raise PortableHistoryError("Turn 't' does not begin with user input")

    monkeypatch.setattr(
        "core.context_preview_estimate._installed_checkpoint_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(checkpoint_eligibility, "render_portable_history", unprojectable)

    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert preview["checkpoint_eligible"] is False
    assert preview["checkpoint_ineligible_reason"] == "history_unprojectable"
    connection.close()


@pytest.mark.parametrize("current_limit", [32000, 64000, 30])
def test_prepared_preview_projects_exact_dispatch_ledger(monkeypatch, current_limit):
    from dataclasses import replace
    from core import context_preview
    from core.context_payload import PreparedContextPayload, RenderedDestinationPayload
    from core.destination_preflight import attach_destination_fit_ledger

    connection, base_thread_row = _preview_connection()
    thread_row = dict(base_thread_row)
    thread_row["model"] = "codex/luna"
    target = _target(harness="codex", model="codex/luna")
    _patch_preview_sources(monkeypatch, target)
    sections = {
        "fixed": 10,
        "checkpoint": 5,
        "tail": 10,
        "current_input": 5,
        "tools": 5,
        "provider_wrapper": 5,
    }
    prepared = PreparedContextPayload(
        mode="exact",
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=1,
        sealed_boundary=None,
        replay=None,
        rendered=RenderedDestinationPayload(
            payload={"provider": "codex", "input": "next"},
            input_tokens=sum(sections.values()),
            accounting_payload={"provider": "codex", "input": "next"},
            fit_section_tokens=sections,
            fit_input_identity="message:next",
            fit_current_input="next",
            fit_components={"current_input": "next"},
        ),
    )
    prepared = attach_destination_fit_ledger(
        prepared,
        harness="codex",
        proven_target_window_tokens=target.context_window,
    )

    # The provider's own capacity rides along so the widget can name a
    # CORSIA budget as one after a fold, exactly as measured rows do (#1753).
    _patch_preview_sources(monkeypatch, replace(
        target, context_window=current_limit, discovered_context_window=1_000_000,
    ))

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread",
        thread_row,
        db=connection,
        current_user_content="next",
        prepared_context=prepared,
    )

    expected = attach_destination_fit_ledger(
        replace(prepared, fit_ledger=None), harness="codex",
        proven_target_window_tokens=current_limit,
    )
    assert preview["preflight"]["status"] == ("allowed" if current_limit >= 40 else "blocked")
    assert preview["preflight"]["destination_fit_ledger"] == (
        expected.fit_ledger.to_dict()
    )
    assert preview["model_context_window"] == current_limit
    assert preview["discovered_context_window"] == 1_000_000
    assert prepared.fit_ledger.effective_window_tokens == 32000
    assert preview["total_used"] == expected.fit_ledger.rendered_input_tokens


def test_checkpoint_preview_ignores_older_measured_receipt(
    isolated_db,
    monkeypatch,
):
    from api import thread_runtime
    from infra.context_receipts import (
        ContextReceiptIdentity,
        attach_context_receipt,
    )

    db, _session = _checkpointed_preview_connection(isolated_db)
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    monkeypatch.setattr(
        "core.dynamic_context_registry.collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=[], agent_facts={}, installed=None, project_cwd=None
        ),
    )
    old_total = 30_000
    receipt = attach_context_receipt(
        "preview-checkpoint",
        "before-answer",
        context_usage={
            "token_usage_available": True,
            "token_source": "measured",
            "total_used": old_total,
            "model_context_window": 32_000,
            "system_tokens": 12_000,
            "tool_def_tokens": 0,
            "history_tokens": 17_000,
            "user_tokens": 0,
            "max_tokens": 1_000,
            "history_messages": 2,
            "continuity_mode": "full",
        },
        identity=ContextReceiptIdentity("native", "native/model", 0),
        db=db,
    )
    assert receipt is not None
    checkpoint = db.execute(
        "SELECT status,through_message_id FROM checkpoints WHERE id='checkpoint-1'"
    ).fetchone()
    assert tuple(checkpoint) == ("installed", "before-answer")
    db.commit()

    monkeypatch.setattr(thread_runtime, "get_db", lambda: db)

    result = thread_runtime._load_next_turn_context_snapshot_preview(
        "preview-checkpoint", "steve"
    )

    assert result["continuity_mode"] == "checkpoint_tail"
    assert result["token_source"] == "estimated"
    assert result["token_usage_available"] is False
    assert result["total_used"] != old_total
    assert result["total_used"] == result["preflight"]["total_tokens"]
    assert result["history_tokens"] == result["preflight"]["history_tokens"]
    assert result["total_used"] == sum(
        result[field]
        for field in (
            "system_tokens",
            "tool_def_tokens",
            "history_tokens",
            "user_tokens",
        )
    )


@pytest.mark.asyncio
async def test_next_turn_preview_requires_auth(monkeypatch):
    from api import thread_runtime

    monkeypatch.setattr(thread_runtime, "authenticate", lambda _token: None)
    run_in_db = AsyncMock()
    monkeypatch.setattr(thread_runtime, "run_in_db", run_in_db)

    with pytest.raises(HTTPException) as error:
        await thread_runtime.next_turn_context_snapshot(
            "preview-thread", token="invalid"
        )

    assert error.value.status_code == 401
    run_in_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_preview_freezes_tools_without_native_singleton(monkeypatch):
    """CC/Codex previews now carry a real tool ledger too (#1952, #806), but
    still must never start or touch the Native agent singleton/MCP connection
    a bare preview request has no business waking."""
    from api import thread_runtime

    definitions = [{
        "type": "function",
        "function": {
            "name": "read_exact",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    calls = []

    async def run_in_db(function, *args):
        calls.append((function, args))
        original = getattr(function, "__wrapped__", function)
        if original is thread_runtime._load_next_turn_context_snapshot_provider:
            return "cc", {"model": "claude-code/sonnet"}, SimpleNamespace()
        if original is thread_runtime.account_capabilities_for_thread:
            return frozenset()
        assert original is thread_runtime._load_next_turn_context_snapshot_preview
        assert args[-3] == "cc"
        assert json.dumps(args[-4], sort_keys=True) == json.dumps(
            definitions, sort_keys=True
        )
        return {"snapshot_id": "ctx-external"}

    monkeypatch.setattr(
        thread_runtime, "authenticate", lambda _token: {"id": "steve"}
    )
    monkeypatch.setattr(thread_runtime, "run_in_db", run_in_db)
    monkeypatch.setattr(
        "infra.runtime_truth.read_runtime_truth", AsyncMock(return_value={}),
    )
    get_agent = AsyncMock(side_effect=AssertionError("started Native tools"))
    monkeypatch.setattr("core.agent.get_agent", get_agent)
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "tools.catalog.advertised_tool_definitions",
        lambda _registry, _capabilities: definitions,
    )

    result = await thread_runtime.next_turn_context_snapshot(
        "preview-thread", token="valid"
    )

    assert result == {"preview": {"snapshot_id": "ctx-external"}}
    assert [getattr(call[0], "__wrapped__", call[0]) for call in calls] == [
        thread_runtime._load_next_turn_context_snapshot_provider,
        thread_runtime.account_capabilities_for_thread,
        thread_runtime._load_next_turn_context_snapshot_preview,
    ]
    get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_preview_freezes_tools_for_same_provider(monkeypatch):
    from api import thread_runtime

    # The preview freezes the same advertised selection a live send uses:
    # the core tool, not the deferred one (#1857).
    definitions = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    deferred = {
        "type": "function",
        "function": {
            "name": "artifact_register",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    agent = SimpleNamespace(
        _connect_mcp=AsyncMock(),
        tools=SimpleNamespace(get_definitions=lambda: [deferred, *definitions]),
    )
    calls = []

    async def run_in_db(function, *args):
        calls.append((function, args))
        original = getattr(function, "__wrapped__", function)
        if original is thread_runtime._load_next_turn_context_snapshot_provider:
            return "native", {"model": "native/model"}, SimpleNamespace()
        if original is thread_runtime.account_capabilities_for_thread:
            return frozenset()
        return {"snapshot_id": "ctx-native"}

    monkeypatch.setattr(
        thread_runtime, "authenticate", lambda _token: {"id": "steve"}
    )
    monkeypatch.setattr(thread_runtime, "run_in_db", run_in_db)
    monkeypatch.setattr(
        "infra.runtime_truth.read_runtime_truth", AsyncMock(return_value={}),
    )
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))

    result = await thread_runtime.next_turn_context_snapshot(
        "preview-thread", token="valid"
    )

    preview_args = calls[2][1]
    assert result == {"preview": {"snapshot_id": "ctx-native"}}
    assert preview_args[-3] == "native"
    assert json.dumps(preview_args[-4], sort_keys=True) == json.dumps(
        definitions, sort_keys=True
    )
    agent._connect_mcp.assert_awaited_once()
