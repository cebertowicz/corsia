"""A checkpoint's tool loop must not release an oversized follow-up request."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_native_checkpoint_aggregate_results_cannot_exceed_selected_window(monkeypatch):
    from core import checkpoint_native_fork as native
    from core.token_estimation import measure_request
    from llm.router import LLMResponse, ToolCallRequest
    from test_checkpoint_provider_adapters import _request, _source

    source = replace(_source(native), prefix_messages=(
        {"role": "user", "content": " x" * 80_000},
    ))
    request = _request(source)
    request = replace(request, target=replace(
        request.target, context_window=131_072, transport_model="openai/gpt-4",
    ))
    writes = []

    async def completion(**kwargs):
        count = measure_request(
            {"messages": kwargs["messages"], "tools": kwargs["tools"]}, target=request.target,
        ).tokens
        assert count + request.max_output_tokens <= request.target.context_window, (
            f"released {count} input tokens without its output budget"
        )
        writes.append(count)
        if len(writes) == 1:
            return LLMResponse(content="Partial checkpoint: verification remains pending.", tool_calls=[
                ToolCallRequest(id=f"call-{index}", name="continuity_edit", arguments={})
                for index in range(16)
            ])
        return LLMResponse(content="checkpoint body")

    async def execute(_name, _arguments):
        return " x" * 4_096  # Each result fits; only their aggregate exceeds the wall.

    async def agent():
        return SimpleNamespace(max_iterations=2, tools=SimpleNamespace(execute=execute))

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda model: {"model": model})
    result = await native.invoke_checkpoint(request, asyncio.Event())
    # The results that overflow the wall are answered with a short error, so the
    # fork's next call still fits and writes its checkpoint (#2514).
    assert len(writes) == 2
    assert result.raw_body == "checkpoint body"
    assert result.diagnostic is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["", "New partial narrative."])
async def test_cutoff_installs_prior_state_and_warning_without_inventing_completion(
    isolated_db, monkeypatch, partial,
):
    from core import checkpoint_runtime as runtime
    from core.checkpoint_render import render_installed_checkpoint
    from core.checkpoint_types import ProviderCheckpointResult
    from infra import checkpoint_queries, checkpoints as store
    from test_checkpoint_runtime import _checkpoint, _provider, _seed

    _seed("cutoff")
    first = replace(_checkpoint("cutoff", "prior"), through_seq=2,
                    through_message_id="cutoff-m2")
    store.create_running(first)
    store.capture("prior", raw_result="Prior decisions and unfinished work.",
                  usage_json=None, captured_at="t2")
    store.install("prior", installed_at="t3")
    wakes = []

    async def invoke(_request, _cancel):
        return ProviderCheckpointResult(partial, {"calls": [{"input_tokens": 123}]},
                                        "checkpoint_context_truncated")

    async def notify(record):
        wakes.append((record.id, record.status))

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "_notify_completion", notify)
    request = replace(_checkpoint("cutoff", "partial"), previous_generation_id="prior")
    await runtime.request_checkpoint(request, _provider("cutoff", "partial"))
    record = await runtime.await_checkpoint("partial")
    if not partial:
        assert record.status == "failed"
        assert record.installed_at is None
        assert checkpoint_queries.read("prior").status == "installed"
        assert wakes == [("partial", "failed")]
        return
    assert record.status == "installed"
    assert record.error == "checkpoint_context_truncated"
    rendered = render_installed_checkpoint(record)
    assert "Prior decisions and unfinished work." in rendered
    if partial:
        assert partial in rendered
    assert "Checkpoint truncated" in rendered
    assert "incomplete" in rendered
    assert wakes == [("partial", "installed")]


@pytest.mark.parametrize("body", ["", "Partial checkpoint narrative."])
@pytest.mark.parametrize("failure_surface", ["result", "stderr"])
def test_cc_context_limit_preserves_partial_output(body, failure_surface):
    from core.checkpoint_cc_fork import _result_after_process

    records = [{"type": "assistant", "message": {"content": body}}]
    failure = "prompt is too long: context_length_exceeded"
    if failure_surface == "result":
        records.append({"type": "result", "is_error": True, "result": failure})
    stdout = b"\n".join(json.dumps(record).encode() for record in records)
    result = _result_after_process(
        stdout, failure if failure_surface == "stderr" else None, 1,
    )
    assert result.raw_body == body
    assert result.diagnostic == "checkpoint_context_truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "Partial checkpoint narrative."])
@pytest.mark.parametrize("prior", ["", "Completed earlier checkpoint fragment."])
async def test_codex_context_limit_preserves_streamed_partial_output(
    monkeypatch, tmp_path, body, prior,
):
    from core import checkpoint_codex_fork as codex
    from test_checkpoint_external_adapters import _request, _target

    terminated = []

    class Session:
        async def start(self):
            if prior:
                await events({"kind": "text_delta", "text": prior})
                await events({"kind": "assistant_text", "text": prior})
            await events({"kind": "text_delta", "text": body})
            await events({"kind": "turn_failed", "message": "context_length_exceeded"})
            return True

        async def terminate(self):
            terminated.append(True)

    def session(*_args, **kwargs):
        nonlocal events
        events = kwargs["on_event"]
        return Session()

    events = None
    monkeypatch.setattr(codex, "_checkpoint_session", session)
    result = await codex._run_app_server_checkpoint(
        _request(_target("codex"), None, binary="unused"), None,
        source_home=tmp_path, isolated_home=tmp_path, cwd=str(tmp_path),
        cancel_event=asyncio.Event(),
        cold_input=SimpleNamespace(input_tokens=10),
    )
    assert result.raw_body == "\n".join(part for part in (prior, body) if part)
    assert result.diagnostic == "checkpoint_context_truncated"
    assert terminated == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_cutoff_without_narrative_and_no_prior_generation_fails_unless_stopped(
    isolated_db, monkeypatch, stop,
):
    from core import checkpoint_runtime as runtime
    from core.checkpoint_types import ProviderCheckpointResult
    from infra.continuity_staging import stage_edit
    from infra.continuity_store import active_entry_snapshot
    from infra.database import get_db
    from test_checkpoint_runtime import _checkpoint, _provider, _seed

    _seed("edit-cutoff")
    wakes = []

    async def invoke(request, cancel):
        db = get_db()
        try:
            stage_edit(db, checkpoint_id=request.checkpoint_id,
                       thread_id=request.thread_id, operation="add", params={
                           "field": "next_actions",
                           "body": {"statement": "Verify the remaining change.",
                                    "source_refs": []},
                       })
            db.commit()
        finally:
            db.close()
        if stop:
            cancel.set()
        return ProviderCheckpointResult("", None, "checkpoint_context_truncated")

    async def notify(record):
        wakes.append(record.status)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "_notify_completion", notify)
    await runtime.request_checkpoint(
        _checkpoint("edit-cutoff", "partial-edit"),
        _provider("edit-cutoff", "partial-edit"),
    )
    record = await runtime.await_checkpoint("partial-edit")
    # A truncated cutoff with no previous generation to carry a narrative
    # forward from still ends up with a blank final body, which must never
    # install (#1767) -- the completed staged edit alone doesn't earn it.
    expected = "cancelled" if stop else "failed"
    assert record.status == expected
    assert wakes == [expected]
    assert record.installed_at is None
    if not stop:
        assert "no content" in record.error
        db = get_db()
        try:
            assert active_entry_snapshot(db, "edit-cutoff", 0) == []
        finally:
            db.close()


@pytest.mark.asyncio
async def test_first_checkpoint_cutoff_without_edits_cannot_advance_coverage(
    isolated_db, monkeypatch,
):
    from core import checkpoint_runtime as runtime
    from core.checkpoint_types import ProviderCheckpointResult
    from test_checkpoint_runtime import _checkpoint, _provider, _seed

    _seed("empty-cutoff")

    async def invoke(_request, _cancel):
        return ProviderCheckpointResult("", None, "checkpoint_context_truncated")

    async def notify(_record):
        pass

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "_notify_completion", notify)
    await runtime.request_checkpoint(
        _checkpoint("empty-cutoff", "empty"), _provider("empty-cutoff", "empty"),
    )
    record = await runtime.await_checkpoint("empty")
    assert record.status == "failed"
    assert record.installed_at is None
    assert "no content" in record.error


@pytest.mark.asyncio
async def test_native_first_input_nonfit_is_not_partial_completion(monkeypatch):
    from unittest.mock import AsyncMock
    from core import checkpoint_native_fork as native
    from core.checkpoint_types import ProviderCheckpointRejected
    from test_checkpoint_provider_adapters import _request, _source

    request = _request(_source(native))
    request = replace(request, target=replace(request.target, context_window=1))
    completion = AsyncMock()
    monkeypatch.setattr(native, "_get_agent", AsyncMock(return_value=SimpleNamespace(max_iterations=2)))
    monkeypatch.setattr(native, "_provider_params", lambda _model: {})
    monkeypatch.setattr(native, "_chat_completion", completion)
    with pytest.raises(ProviderCheckpointRejected, match="initial.*input"):
        await native.invoke_checkpoint(request, asyncio.Event())
    completion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("normalized", [False, True])
async def test_native_provider_context_limit_is_degraded_not_error_prose(monkeypatch, normalized):
    from core import checkpoint_native_fork as native
    from llm.router import LLMResponse, ToolCallRequest
    from test_checkpoint_provider_adapters import _request, _source
    from llm.provider_overflow import context_overflow_response

    responses = [
        LLMResponse(content="Retain this partial checkpoint.", tool_calls=[
            ToolCallRequest(id="edit", name="edit", arguments={}),
        ]),
        context_overflow_response() if normalized else
        LLMResponse(content="context_length_exceeded", finish_reason="error"),
    ]

    async def completion(**_kwargs):
        return responses.pop(0)

    async def execute(*_args):
        return "edit completed"

    async def agent():
        return SimpleNamespace(max_iterations=3, tools=SimpleNamespace(execute=execute))

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {})
    result = await native.invoke_checkpoint(_request(_source(native)), asyncio.Event())
    assert result.raw_body == "Retain this partial checkpoint."
    assert result.diagnostic == "checkpoint_context_truncated"
    assert responses == []
