"""The generic LiteLLM transport and the OpenAI Responses adapter feed the same tool-input sink as Anthropic."""
import json
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute
from tests.test_live_preview_attempts import _Stream

ARGS = json.dumps({"doc_path": "a.md", "quote": "fox", "replacement": "wolf"})


class Sink:
    def __init__(self):
        self.received: list[tuple[str, str, str]] = []
        self.abandoned: list[str] = []

    def on_tool_input(self, call_id, name, fragment):
        self.received.append((call_id, name, fragment))

    def on_tool_abandoned(self, call_id):
        self.abandoned.append(call_id)

    def text(self, call_id):
        return "".join(fragment for c, _name, fragment in self.received if c == call_id)


@pytest.fixture
def sink():
    from llm.tool_input_stream import tool_input_sink

    sink = Sink()
    binding = tool_input_sink.set(sink)
    yield sink
    tool_input_sink.reset(binding)


def chat_delta(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def chat_chunk(*tool_deltas, finish=None):
    delta = SimpleNamespace(content=None, reasoning_content=None, tool_calls=list(tool_deltas) or None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


def chat_chunks(call_id, index, name, args, *, size=6):
    pieces = [args[i:i + size] for i in range(0, len(args), size)]
    return [chat_chunk(chat_delta(index, call_id=call_id, name=name, arguments=""))] + [
        chat_chunk(chat_delta(index, arguments=piece)) for piece in pieces]


async def test_litellm_streams_each_calls_input_once_its_id_and_name_are_known(sink):
    from llm.base_streaming import _consume_tool_call_deltas

    calls, emitted = {}, set()
    # The name arrives after the first fragment: that backlog is sent once, in order, as soon as it does.
    late = [chat_delta(1, call_id="call-2", arguments=ARGS[:5]), chat_delta(1, name="doc_comment_create"),
            *[chat_delta(1, arguments=ARGS[i:i + 5]) for i in range(5, len(ARGS), 5)]]
    early = [chunk.choices[0].delta.tool_calls[0] for chunk in chat_chunks("call-1", 0, "edit_file", ARGS)]
    for delta in [item for pair in zip(early, late) for item in pair] + early[len(late):] + late[len(early):]:
        await _consume_tool_call_deltas([delta], calls, emitted, None)
    assert sink.text("call-1") == ARGS and sink.text("call-2") == ARGS
    assert {(c, name) for c, name, _f in sink.received} == {("call-1", "edit_file"), ("call-2", "doc_comment_create")}


async def _litellm_attempt(chunks, tail):
    from llm.base_streaming import streaming_completion

    async def acompletion(**_kwargs):
        return _Stream(chunks, tail)

    async def ignore(*_args):
        return None

    return await streaming_completion({}, "openai/test", ignore, ignore, ignore, acompletion_fn=acompletion)


async def test_litellm_abandons_a_call_whose_attempt_fails_mid_input(sink):
    with pytest.raises(RuntimeError):
        await _litellm_attempt(chat_chunks("call-1", 0, "doc_comment_create", ARGS)[:3], "raise")
    assert sink.abandoned == ["call-1"]


async def test_litellm_keeps_a_returned_call(sink):
    chunks = chat_chunks("call-1", 0, "doc_comment_create", ARGS) + [chat_chunk(finish="tool_calls")]
    response = await _litellm_attempt(chunks, "end")
    assert [call.id for call in response.tool_calls] == ["call-1"]
    assert sink.text("call-1") == ARGS and sink.abandoned == []


def responses_events(call_id, index, name, args, *, size=6):
    added = SimpleNamespace(type="response.output_item.added", output_index=index,
                            item=SimpleNamespace(type="function_call", call_id=call_id, id=f"fc_{index}", name=name, arguments=""))
    deltas = [SimpleNamespace(type="response.function_call_arguments.delta", output_index=index, item_id=f"fc_{index}",
                              delta=args[i:i + size]) for i in range(0, len(args), size)]
    return [added, *deltas]


async def test_responses_streams_input_under_the_call_id_not_the_item_id(sink):
    from llm.providers.openai_streaming import _consume_tool_call_event

    calls, emitted = {}, set()
    for event in responses_events("call_abc", 0, "doc_comment_create", ARGS):
        await _consume_tool_call_event(event, event.type, {}, calls, emitted, None)
    # Arguments for an item whose header never arrived cannot be keyed to the call that will run.
    orphan = SimpleNamespace(type="response.function_call_arguments.delta", output_index=1, item_id="fc_1", delta="{")
    await _consume_tool_call_event(orphan, orphan.type, {}, calls, emitted, None)
    assert sink.text("call_abc") == ARGS
    assert {c for c, _name, _f in sink.received} == {"call_abc"}


async def test_responses_abandons_a_call_whose_attempt_fails_mid_input(sink):
    from llm.providers.openai_streaming import _streaming_responses_completion

    stream = _Stream(responses_events("call_abc", 0, "doc_comment_create", ARGS)[:3], "raise")

    async def create(**_kwargs):
        return stream

    async def ignore(*_args):
        return None

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(RuntimeError):
        await _streaming_responses_completion(client, {}, model="gpt-test", on_reasoning_chunk=ignore,
                                              on_content_chunk=ignore, on_tool_call_chunk=ignore)
    assert sink.abandoned == ["call_abc"]


async def test_a_native_turn_previews_a_comment_streamed_through_litellm(monkeypatch):
    from core.turn_provider_call import _StreamingCallbacks
    from llm.base_streaming import _consume_tool_call_deltas
    from llm.tool_input_stream import tool_input_sink

    route = PreviewRoute()
    monkeypatch.setattr(live_preview, "previews", route)

    async def on_step(*_args):
        return None

    binding = tool_input_sink.set(_StreamingCallbacks(on_step, "thread-a"))
    try:
        calls, emitted = {}, set()
        for chunk in chat_chunks("call-1", 0, "doc_comment_create", ARGS)[:2]:
            await _consume_tool_call_deltas(chunk.choices[0].delta.tool_calls, calls, emitted, None)
    finally:
        tool_input_sink.reset(binding)
    assert set(route.calls) == {"call-1"} and route.calls["call-1"].tool == "doc_comment_create"
