
import asyncio
import pytest

from delegation_fixtures import deleg_db  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_mock_state():
    from e2e_mock import _reset_mock_state
    _reset_mock_state()
    yield
    _reset_mock_state()


def test_mock_engine_restores_patches_when_body_raises(monkeypatch):
    from llm import base, model_status
    from e2e_mock import mock_engine

    original_completion = base.acompletion
    production_providers = lambda: {"anthropic"}
    monkeypatch.setattr(model_status, "_cloud_providers_active", production_providers)

    with pytest.raises(RuntimeError, match="deliberate"):
        with mock_engine():
            assert base.acompletion is not original_completion
            assert model_status._cloud_providers_active() == {"anthropic", "mock"}
            raise RuntimeError("deliberate")

    assert base.acompletion is original_completion
    assert model_status._cloud_providers_active is production_providers


def test_default_reply():
    from e2e_mock import _next_reply, _DEFAULT_REPLY
    reply = _next_reply()
    assert reply["content"] == _DEFAULT_REPLY["content"]
    assert reply["role"] == "assistant"


def test_seeded_replies_consumed_in_order():
    from e2e_mock import _reset_mock_state, _next_reply

    _reset_mock_state([
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "second"},
    ])

    assert _next_reply()["content"] == "first"
    assert _next_reply()["content"] == "second"
    assert _next_reply()["content"] == "second"
    assert _next_reply()["content"] == "second"


@pytest.mark.asyncio
async def test_stream_targeted_replies_do_not_depend_on_call_arrival_order():
    """Concurrent checkpoint and turn calls receive replies for their own shape."""
    from e2e_mock import _mock_acompletion, _reset_mock_state

    _reset_mock_state([
        {"content": "CHECKPOINT", "match_stream": False},
        {"content": "TURN", "match_stream": True},
    ])

    turn_stream = await _mock_acompletion(model="test", messages=[], stream=True)
    turn_chunks = [chunk async for chunk in turn_stream]
    turn_text = "".join(
        chunk.choices[0].delta.content or "" for chunk in turn_chunks
    )
    checkpoint = await _mock_acompletion(model="test", messages=[], stream=False)

    assert turn_text == "TURN"
    assert checkpoint.choices[0].message.content == "CHECKPOINT"


def test_thread_targeted_replies_ignore_other_threads():
    """A thread left running by an earlier test neither takes nor counts the seeded replies."""
    import e2e_mock
    from infra import state

    e2e_mock._reset_mock_state([
        {"content": "first", "match_thread": "writer"},
        {"content": "second", "match_thread": "writer"},
    ])

    def call_as(thread_id):
        bound = state._current_thread_id.set(thread_id)
        try:
            return e2e_mock._next_reply(stream=True)
        finally:
            state._current_thread_id.reset(bound)

    assert call_as("stale") == e2e_mock._DEFAULT_REPLY
    assert call_as(None) == e2e_mock._DEFAULT_REPLY
    assert call_as("writer")["content"] == "first"
    assert call_as("stale") == e2e_mock._DEFAULT_REPLY
    assert call_as("writer")["content"] == "second"
    assert call_as("writer")["content"] == "second"
    assert e2e_mock._call_count == 3


def test_calls_from_unseeded_threads_are_logged_and_listed():
    """The default reply a thread left running gets is never silent: the log and the inspection endpoint show it."""
    import asyncio

    import e2e_mock
    from infra import state
    from loguru import logger

    e2e_mock._reset_mock_state([{"content": "first", "match_thread": "writer"}])
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    bound = state._current_thread_id.set("stale")
    try:
        assert e2e_mock._next_reply(stream=True) == e2e_mock._DEFAULT_REPLY
    finally:
        state._current_thread_id.reset(bound)
        logger.remove(sink)
    assert any("thread stale" in line for line in seen)
    assert asyncio.run(e2e_mock.read_mock_response_state()) == {
        "call_count": 0, "unmatched_calls": [{"thread_id": "stale", "stream": True}]}
    e2e_mock._reset_mock_state([])
    assert asyncio.run(e2e_mock.read_mock_response_state()) == {"call_count": 0, "unmatched_calls": []}


def test_reset_replaces_queue():
    from e2e_mock import _reset_mock_state, _next_reply

    _reset_mock_state([{"role": "assistant", "content": "A"}])
    assert _next_reply()["content"] == "A"

    _reset_mock_state([{"role": "assistant", "content": "B"}])
    assert _next_reply()["content"] == "B"


@pytest.mark.asyncio
async def test_streaming_produces_correct_chunks():
    from e2e_mock import _streaming_iter

    reply = {
        "content": "Hello world",
        "reasoning_content": "Let me think...",
        "finish_reason": "stop",
    }

    chunks = []
    async for chunk in _streaming_iter(reply):
        c = chunk.choices[0]
        delta = c.delta
        if delta.reasoning_content is not None:
            chunks.append(("reasoning", delta.reasoning_content))
        elif delta.content is not None:
            chunks.append(("content", delta.content))
        elif c.finish_reason is not None:
            chunks.append(("finish", c.finish_reason))

    types = [c[0] for c in chunks]
    assert types.index("reasoning") < types.index("content")
    assert types.index("content") < types.index("finish")

    reasoning_text = "".join(c[1] for c in chunks if c[0] == "reasoning")
    assert reasoning_text == "Let me think..."

    content_text = "".join(c[1] for c in chunks if c[0] == "content")
    assert content_text == "Hello world"

    finish = [c[1] for c in chunks if c[0] == "finish"]
    assert finish == ["stop"]


@pytest.mark.asyncio
async def test_streaming_with_delay():
    from e2e_mock import _streaming_iter
    import time

    reply = {"content": "fast", "delay_ms": 200}

    start = time.monotonic()
    chunks = []
    async for chunk in _streaming_iter(reply):
        chunks.append(chunk)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.15  # at least ~150ms (allowing margin)
    assert len(chunks) >= 2  # content + finish


@pytest.mark.asyncio
async def test_non_streaming_response():
    from e2e_mock import _mock_acompletion, _reset_mock_state

    _reset_mock_state([{"role": "assistant", "content": "Hello"}])

    result = await _mock_acompletion(model="test", messages=[], stream=False)
    assert result.choices[0].message.content == "Hello"
    assert result.choices[0].finish_reason == "stop"
    assert result.usage.total_tokens == 150


@pytest.mark.asyncio
async def test_streaming_response():
    from e2e_mock import _mock_acompletion, _reset_mock_state

    _reset_mock_state([{"role": "assistant", "content": "Hi there"}])

    result = _mock_acompletion(model="test", messages=[], stream=True)
    gen = await result
    chunks = [chunk async for chunk in gen]
    assert len(chunks) >= 2  # at least content + finish


@pytest.mark.asyncio
async def test_tool_calls_in_stream():
    from e2e_mock import _streaming_iter

    reply = {
        "content": None,
        "tool_calls": [
            {"id": "tc_1", "name": "search", "arguments": '{"q": "test"}'},
        ],
        "finish_reason": "tool_calls",
    }

    chunks = []
    async for chunk in _streaming_iter(reply):
        c = chunk.choices[0]
        if c.delta.tool_calls:
            chunks.append(("tool_call", c.delta.tool_calls[0].function.name))
        elif c.finish_reason:
            chunks.append(("finish", c.finish_reason))

    assert ("tool_call", "search") in chunks
    assert ("finish", "tool_calls") in chunks


@pytest.mark.asyncio
async def test_completion_gate_blocks_until_released():
    from e2e_mock import (
        _next_reply,
        _release_mock_gate,
        _reset_mock_state,
        _streaming_iter,
    )

    _reset_mock_state(
        [
            {
                "role": "assistant",
                "content": "go",
                "reasoning_content": "think",
                "completion_gate": "turn-a",
            },
        ]
    )
    stream = _streaming_iter(_next_reply())

    assert (await anext(stream)).choices[0].delta.reasoning_content == "th"
    assert (await anext(stream)).choices[0].delta.reasoning_content == "ink"

    content_chunk = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not content_chunk.done()

    assert _release_mock_gate("turn-a")
    assert (await content_chunk).choices[0].delta.content == "g"


@pytest.mark.asyncio
async def test_response_gate_holds_the_stream_until_released():
    """The call is counted (the write was sent) but no stream opens, so the
    caller cannot reach response-started acceptance before the release."""
    import e2e_mock
    from e2e_mock import _mock_acompletion, _release_mock_gate, _reset_mock_state

    _reset_mock_state([{"role": "assistant", "content": "go", "response_gate": "open"}])
    call = asyncio.create_task(_mock_acompletion(stream=True))
    await asyncio.sleep(0)
    assert e2e_mock._call_count == 1
    assert not call.done()

    assert _release_mock_gate("open")
    stream = await call
    assert (await anext(stream)).choices[0].delta.content == "g"


def test_reset_removes_completion_gates():
    from e2e_mock import _release_mock_gate, _reset_mock_state

    _reset_mock_state([{"completion_gate": "old-gate"}])
    assert _release_mock_gate("old-gate")

    _reset_mock_state()
    assert not _release_mock_gate("old-gate")


@pytest.mark.asyncio
async def test_reset_releases_old_gate_waiter_and_keeps_new_gate_closed():
    from e2e_mock import (
        _next_reply,
        _release_mock_gate,
        _reset_mock_state,
        _streaming_iter,
    )

    _reset_mock_state([{
        "content": "old",
        "reasoning_content": "think",
        "completion_gate": "old-gate",
    }])
    old_stream = _streaming_iter(_next_reply())
    await anext(old_stream)
    await anext(old_stream)
    old_content = asyncio.create_task(anext(old_stream))
    await asyncio.sleep(0)
    assert not old_content.done()

    _reset_mock_state([{
        "content": "new",
        "completion_gate": "new-gate",
    }])
    assert (await asyncio.wait_for(old_content, timeout=0.1)).choices[0].delta.content == "o"

    new_stream = _streaming_iter(_next_reply())
    new_content = asyncio.create_task(anext(new_stream))
    await asyncio.sleep(0)
    assert not new_content.done()

    assert _release_mock_gate("new-gate")
    assert (await new_content).choices[0].delta.content == "n"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}])
async def test_inject_message_binds_a_fixture_assistant_row_to_no_turn(
    deleg_db, monkeypatch, metadata,
):
    import json

    from e2e_mock import inject_message
    from infra import auth, state

    monkeypatch.setattr(auth, "authenticate", lambda _arg: {"id": "u-test"})
    monkeypatch.setattr(state, "broadcast_to_thread", lambda *_args: None)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('inject-1','u-test','claude-code/opus','T','t','t')"
    )
    deleg_db.commit()
    message = {"role": "assistant", "content": "fixture"}
    if metadata is not None:
        message["metadata"] = metadata

    result = await inject_message("x", {"thread_id": "inject-1", "message": message})

    row = deleg_db.execute(
        "SELECT metadata FROM messages WHERE id=?", (result["message_id"],)
    ).fetchone()
    assert json.loads(row["metadata"]) == {"turn_unbound_reason": "legacy_import"}
