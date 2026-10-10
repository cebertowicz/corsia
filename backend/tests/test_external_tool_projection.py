"""Final tool replies use current-call occupancy, not a reconstructed checkpoint tail."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from subagents.codex_app_server import CodexAppServerSession
from test_checkpoint_external_adapters import _target


def target(window=131_072):
    return replace(_target("codex"), context_window=window,
                   transport_model="gpt-4", expected_reported_model="gpt-4")


@pytest.mark.asyncio
async def test_claude_steer_write_binds_count_to_echoed_uuid_without_changing_text():
    import json

    session = _claude_session()
    writes = []
    async def drain():
        return None
    session._proc.stdin = SimpleNamespace(write=writes.append, drain=drain)
    await session.feed("same text", input_context=("first", 30))
    await session.feed("same text", input_context=("second", 40))
    assert session.tool_context.pending_inputs == {"first": 30, "second": 40}
    frames = [json.loads(frame) for frame in writes]
    assert [frame["uuid"] for frame in frames] == ["first", "second"]
    assert [frame["message"]["content"] for frame in frames] == ["same text", "same text"]


@pytest.mark.asyncio
async def test_codex_steer_count_waits_for_consumption_not_rpc_acceptance(monkeypatch):
    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread")
    session._track_active_turn("turn")
    sent = []
    async def request(method, params, **callbacks):
        sent.append((method, params))
        callbacks["on_write_observed"]()
        return {"turnId": "turn"}
    monkeypatch.setattr(session, "_request", request)
    assert await session.steer("same text", input_context=("first", 30))
    assert session.tool_context.pending_inputs == {"first": 30}
    assert not session.tool_context.consumed_inputs
    assert sent[0][1]["clientUserMessageId"] == "first"
    assert sent[0][1]["input"][0]["text"] == "same text"
    await session._handle_notification("item/started", {
        "threadId": "thread", "turnId": "turn",
        "item": {"id": "item", "type": "userMessage", "clientId": "first"},
    })
    assert session.tool_context.consumed_inputs == {"first"}
    await session._handle_notification("thread/tokenUsage/updated", {
        "threadId": "thread", "turnId": "turn", "tokenUsage": {
            "last": {"inputTokens": 100, "outputTokens": 10},
            "total": {"inputTokens": 100, "outputTokens": 10},
        },
    })
    assert session.tool_context.pending_inputs == {}
    assert session.tool_context.tokens == 110


@pytest.mark.asyncio
async def test_claude_steer_ack_retires_only_inputs_in_the_covering_receipt():
    session = _claude_session()
    await _claude_response(session)
    context = session.tool_context
    context.pending_inputs["first"] = 30
    context.pending_inputs["second"] = 40
    assert context.tokens == 9770
    # Both were written before this start, but neither was consumed by it.
    await _claude_response(session, message_id="in-flight", input_tokens=9700)
    assert context.tokens == 10470
    await session._dispatch_raw_event({"type": "user", "uuid": "first"})
    # A duplicate receipt does not cover this newly acknowledged input.
    await _claude_response(session, message_id="in-flight", input_tokens=9700)
    assert context.tokens == 10470
    await _claude_response(session, message_id="cover-first", input_tokens=10430)
    assert context.pending_inputs == {"second": 40}
    assert context.tokens == 11170


@pytest.mark.asyncio
async def test_claude_anchor_uuid_leaves_provider_request_bytes_unchanged():
    """Only the frame's own ``uuid`` differs; the model-visible message is identical."""
    import json

    session = _claude_session()
    writes = []
    async def drain():
        return None
    session._proc.stdin = SimpleNamespace(write=writes.append, drain=drain)
    await session.feed("same text")
    await session.feed("same text", input_id="anchor-row")
    plain, anchored = (json.loads(frame) for frame in writes)
    assert anchored.pop("uuid") == "anchor-row"
    assert anchored == plain
    assert session.tool_context.pending_inputs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("turn/start", "turn/steer"))
async def test_codex_anchor_id_leaves_provider_request_bytes_unchanged(monkeypatch, method):
    """``clientUserMessageId`` is the only added field on turn start and steer."""
    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread")
    sent = []
    async def request(name, params, **callbacks):
        sent.append(dict(params))
        return {"turn": {"id": "turn"}, "turnId": "turn"}
    monkeypatch.setattr(session, "_request", request)
    for client_message_id in (None, "anchor-row"):
        if method == "turn/start":
            await session.start_turn("same text", client_message_id=client_message_id)
        else:
            session._track_active_turn("turn")
            await session.steer("same text", client_message_id=client_message_id)
    plain, anchored = sent
    assert anchored.pop("clientUserMessageId") == "anchor-row"
    assert anchored == plain


def test_codex_steer_consumption_and_duplicate_usage_do_not_clear_pending_inputs():
    from core.checkpoint_codex_context import CodexCheckpointContext

    context = CodexCheckpointContext(target(), 100)
    def receipt(ordinal):
        return {"last": {"inputTokens": 100, "outputTokens": 10},
                "total": {"inputTokens": ordinal * 100, "outputTokens": ordinal * 10}}
    context.record_receipt(receipt(1))
    context.pending_inputs.update({"first": 30, "second": 40})
    assert context.tokens == 180
    context.consumed_inputs.add("first")
    context.record_receipt(receipt(1))
    assert context.tokens == 180
    context.record_receipt(receipt(2))
    assert context.pending_inputs == {"second": 40}
    assert context.tokens == 150


def _claude_session():
    from subagents.cc_stream import CCStreamSession

    session = CCStreamSession(binary="unused", session_id="fixture")
    process = SimpleNamespace(
        returncode=None,
        stdout=SimpleNamespace(at_eof=lambda: False),
    )
    process.kill = lambda: setattr(process, "returncode", -9)
    session._proc = process
    return session


async def _claude_response(session, *, message_id="call", input_tokens=9000, finish=True):
    for event in [
        {"type": "message_start", "message": {"id": message_id, "usage": {
            "input_tokens": input_tokens - 8000, "cache_read_input_tokens": 7000,
            "cache_creation_input_tokens": 1000, "output_tokens": 1,
        }}},
        {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "edit"}},
    ]:
        await session._dispatch_raw_event({"type": "stream_event", "event": event})
    if finish:
        await _claude_finish(session)


async def _claude_finish(session):
    for event in [{"type": "message_delta", "usage": {"output_tokens": 700}}, {"type": "message_stop"}]:
        await session._dispatch_raw_event({"type": "stream_event", "event": event})


@pytest.mark.asyncio
async def test_claude_reply_waits_for_current_full_output_then_aggregates_actual_media():
    import asyncio

    session = _claude_session()
    await _claude_response(session, finish=False)
    task = asyncio.create_task(session.record_tool_reply("edit", " x" * 100, target()))
    await asyncio.sleep(0)
    assert not task.done()
    await _claude_finish(session)
    await task
    assert session.tool_context.tokens == 9804

    class Reply(str):
        media = SimpleNamespace(image=object())

    await session.record_tool_reply("edit", Reply(" x" * 20), target())
    assert session.tool_context.tokens == 10828
    # A fallback media object without an actual image adds no phantom image.
    Reply.media = SimpleNamespace(image=None)
    await session.record_tool_reply("edit", Reply("x"), target())
    assert session.tool_context.tokens == 10833
    await _claude_response(session, message_id="next", input_tokens=10833)
    await session.record_tool_reply("edit", "x", target())
    assert session.tool_context.tokens == 11538


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_claude_missing_current_receipt_waits_until_stop_or_process_exit(stop):
    import asyncio

    session = _claude_session()
    await _claude_response(session)  # An unrelated completed call is insufficient.
    task = asyncio.create_task(session.record_tool_reply("foreign", "not released", target()))
    await asyncio.sleep(0)
    assert not task.done()
    if stop:
        session._request_terminate()
    else:
        session._proc.returncode = 1
        session._context_changed.set()
    with pytest.raises(RuntimeError, match="context|ended"):
        await asyncio.wait_for(task, 1)
    assert session._proc.returncode == (-9 if stop else 1)
    assert session._closing is stop  # Only explicit Stop suppresses natural exit ownership.
    assert session.tool_context.results == 0


@pytest.mark.asyncio
async def test_claude_output_only_final_delta_keeps_current_input_and_cache_receipt():
    from subagents.cc_event_context_usage import context_usage_from_final_call

    session = _claude_session()
    events = []

    async def observe(event):
        events.append(event)

    session._on_event = observe
    await _claude_response(session)
    final = next(event for event in events if event["kind"] == "final_call_usage")
    assert final["usage"] == {"input_tokens": 1000, "cache_read_input_tokens": 7000,
                              "cache_creation_input_tokens": 1000, "output_tokens": 700}
    measured = context_usage_from_final_call(final["usage"], selected_context_window=8000)
    assert measured["total_used"] == 9000
    assert measured["utilization"] == 1.125


@pytest.mark.asyncio
async def test_claude_eof_wakes_receipt_wait_after_process_wait_completes():
    import asyncio
    from unittest.mock import AsyncMock

    session = _claude_session()
    release = asyncio.Event()

    async def process_wait():
        await release.wait()
        session._proc.returncode = 0

    session._proc.wait = process_wait
    session._proc.stderr = asyncio.StreamReader()
    session._proc.stderr.feed_eof()
    session._on_exit = AsyncMock()
    reply = asyncio.create_task(session.record_tool_reply("missing", "not released", target()))
    finish = asyncio.create_task(session._finish_reader(None))
    await asyncio.sleep(0)
    release.set()
    await finish
    with pytest.raises(RuntimeError, match="ended"):
        await asyncio.wait_for(reply, 1)
    session._on_exit.assert_awaited_once_with(0, "")


def test_codex_actual_replies_include_media_and_aggregate_until_the_next_receipt():
    session = CodexAppServerSession(binary="unused", task=None,
        corsia_thread_id="thread", tool_registry=object())
    session._active_turn_id = "turn"
    session._usage_by_turn["turn"] = {
        "last": {"inputTokens": 9000, "outputTokens": 700},
        "total": {"inputTokens": 19000000, "outputTokens": 20000},
    }
    counter = session._tool_requests
    # 100 text tokens, the existing 1000 image estimate, and 4 framing tokens.
    assert counter.measure_tool_reply({"contentItems": [
        {"type": "inputText", "text": " x" * 100},
        {"type": "inputImage", "imageUrl": "fixture://image"},
    ]}, target()) == 10804
    assert counter.measure_tool_reply({"contentItems": [
        {"type": "inputText", "text": " x" * 20},
    ]}, target()) == 10828
    session._usage_by_turn["turn"] = {
        "last": {"inputTokens": 10828, "outputTokens": 3},
        "total": {"inputTokens": 19010828, "outputTokens": 20003},
    }
    assert counter.measure_tool_reply({"contentItems": [
        {"type": "inputText", "text": "x"},
    ]}, target()) == 10836


@pytest.mark.asyncio
async def test_complete_reply_count_replaces_checkpoint_style_tail_at_admission(monkeypatch):
    from core.checkpoint_resume import CheckpointWaitScope
    from subagents.harness_checkpoint_context import ExternalCheckpointContext
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    current_target = target(20000)
    context = ExternalCheckpointContext(thread_row={}, target=current_target,
        history=object(), boundary=None, previous_generation_id=None,
        working_dir="/tmp", tail_tokens=19000, minimum_tail_tokens=1000)
    live = SimpleNamespace(catalog_model_id=current_target.catalog_id,
        corsia_turn=SimpleNamespace(turn_id="turn"), checkpoint_wait_scope=CheckpointWaitScope(),
        context_window=20000, session=SimpleNamespace(peek_active_usage=lambda: {
            "input_tokens": 9000, "model_context_window": 20000,
        }))
    monkeypatch.setattr("subagents.harness_checkpoint_resume.read_external_checkpoint_context",
                        lambda *_a, **_k: context)
    # Actual complete input fits. Adding the unrelated 19000-token checkpoint
    # representation would incorrectly enter the no-checkpoint failure path.
    assert not await maintain_external_tool_boundary(
        live, "thread", "codex", complete_input_tokens=10828,
    )
