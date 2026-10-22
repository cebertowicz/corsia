"""Claude Code and the native Anthropic adapter feed the preview route; a write names the call it ends."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute


class Recorder:
    """The preview route's interface, recording calls instead of drawing."""

    def __init__(self):
        self.calls: list[tuple] = []

    def start(self, call_id, **kwargs):
        self.calls.append(("start", call_id, kwargs["tool"], kwargs["turn_id"]))

    def update(self, call_id, fragment):
        self.calls.append(("update", call_id, fragment))

    def end(self, call_id, outcome):
        self.calls.append(("end", call_id, outcome))

    def end_where(self, outcome, *, thread_id=None, turn_id=None):
        self.calls.append(("end_where", outcome, thread_id, turn_id))


def raw_stream(event: dict, parent: str | None = None) -> dict:
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def cc_frames(call_id: str, name: str, index: int, args: dict) -> list[dict]:
    raw = json.dumps(args)
    frames = [raw_stream({"type": "content_block_start", "index": index,
                          "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}})]
    frames += [raw_stream({"type": "content_block_delta", "index": index,
                           "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 7]}})
               for i in range(0, len(raw), 7)]
    return frames + [raw_stream({"type": "content_block_stop", "index": index})]


def test_claude_code_tool_input_normalizes_by_block_index():
    from subagents.cc_events import normalize_event

    events = [event for frame in cc_frames("toolu_1", "mcp__corsia__edit_file", 2, {"path": "a"})
              for event in normalize_event(frame)]
    assert events[0] == {"kind": "tool_input_start", "index": 2, "call_id": "toolu_1", "name": "mcp__corsia__edit_file"}
    assert "".join(event["text"] for event in events[1:-1]) == json.dumps({"path": "a"})
    assert events[-1] == {"kind": "tool_input_stop", "index": 2}


def test_a_subagents_own_stream_is_not_previewed():
    from subagents.cc_events import normalize_event

    frames = cc_frames("toolu_1", "mcp__corsia__edit_file", 0, {"path": "a"})
    assert [normalize_event({**frame, "parent_tool_use_id": "toolu_task"}) for frame in frames] == [[]] * len(frames)


def cc_context(monkeypatch):
    from subagents import cc_event_preview
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    recorder = Recorder()
    monkeypatch.setattr(cc_event_preview, "previews", recorder)
    live = _Live(session=None, parent_thread_id=None, name="agent")
    live.tool_registry = SimpleNamespace(get=lambda name: SimpleNamespace(workspace=None))
    live.corsia_turn = SimpleNamespace(turn_id="turn-1")
    return CCEventContext(live=live, child_id="thread-a", bcast=lambda *_: None, db=None), recorder


async def test_claude_code_handlers_feed_the_route_without_the_database(monkeypatch):
    from subagents import cc_event_preview as handlers

    def no_database(*_args, **_kwargs):
        raise AssertionError("a preview handler touched the database")

    monkeypatch.setattr("infra.db_async.run_in_db", no_database)
    monkeypatch.setattr("infra.database.get_db", no_database)
    ctx, recorder = cc_context(monkeypatch)
    await handlers.on_tool_input_start(ctx, {"index": 0, "call_id": "toolu_1", "name": "mcp__corsia__multi_edit"})
    await handlers.on_tool_input_start(ctx, {"index": 1, "call_id": "toolu_2", "name": "mcp__corsia__bash"})
    await handlers.on_tool_input_delta(ctx, {"index": 0, "text": '{"pa'})
    await handlers.on_tool_input_delta(ctx, {"index": 1, "text": '{"command"'})
    await handlers.on_tool_input_stop(ctx, {"index": 0})
    await handlers.on_tool_input_delta(ctx, {"index": 0, "text": "late"})
    assert recorder.calls == [("start", "toolu_1", "multi_edit", "turn-1"), ("update", "toolu_1", '{"pa'),
                              ("update", None, '{"command"'), ("update", None, "late")]
    assert ctx.live.preview_blocks == {}


async def test_claude_code_results_end_their_previews(monkeypatch):
    from subagents.cc_event_preview import ending_call, ending_thread

    ctx, recorder = cc_context(monkeypatch)
    seen = []

    async def handler(_ctx, event):
        seen.append(event)
        return "settlement"

    assert await ending_call(handler)(ctx, {"tool_use_id": "toolu_1", "is_error": True}) == "settlement"
    ctx.live.preview_blocks[3] = "toolu_2"
    assert await ending_thread(handler, "turn_ended")(ctx, {}) == "settlement"
    assert recorder.calls == [("end", "toolu_1", "failed"), ("end_where", "turn_ended", "thread-a", None)]
    assert ctx.live.preview_blocks == {} and len(seen) == 2


def test_the_live_claude_code_pump_dispatches_tool_input():
    from subagents.cc_runtime_events import _dispatch

    assert {"tool_input_start", "tool_input_delta", "tool_input_stop"} <= set(_dispatch())


def anthropic_events(call_id: str, index: int, args: dict) -> list:
    raw = json.dumps(args)
    start = SimpleNamespace(type="content_block_start", index=index,
                            content_block=SimpleNamespace(type="tool_use", id=call_id, name="edit_file", input={}))
    deltas = [SimpleNamespace(type="content_block_delta", index=index,
                              delta=SimpleNamespace(type="input_json_delta", partial_json=raw[i:i + 5]))
              for i in range(0, len(raw), 5)]
    return [start, *deltas, SimpleNamespace(type="content_block_stop", index=index)]


async def test_the_native_adapter_streams_input_and_starts_each_tool_once():
    from llm.providers.anthropic_streaming import _consume_stream_event
    from llm.tool_input_stream import tool_input_sink

    received, starts = [], []

    async def on_tool_call_chunk(*args):
        starts.append(args[1])

    state = dict(text_parts=[], reasoning_parts=[], thinking_blocks_by_index={}, redacted_blocks_by_index={},
                 tool_calls_by_index={}, emitted_tool_indices=set(), finish_reason="stop", usage={},
                 reported_model=None, on_reasoning_chunk=None, on_content_chunk=None,
                 on_tool_call_chunk=on_tool_call_chunk)
    one, two = anthropic_events("call-1", 0, {"path": "a.md"}), anthropic_events("call-2", 1, {"path": "b.md"})
    binding = tool_input_sink.set(SimpleNamespace(on_tool_input=lambda *args: received.append(args)))
    try:
        for event in [item for pair in zip(one, two) for item in pair] + one[len(two):] + two[len(one):]:
            await _consume_stream_event(event, **state)
    finally:
        tool_input_sink.reset(binding)
    assert starts == ["call-1", "call-2"]
    for call_id, path in (("call-1", "a.md"), ("call-2", "b.md")):
        assert "".join(f for c, name, f in received if c == call_id) == json.dumps({"path": path})
        assert {name for c, name, _f in received if c == call_id} == {"edit_file"}


async def test_unbound_native_streams_send_nothing():
    from llm.tool_input_stream import send_tool_input, tool_input_sink

    assert tool_input_sink.get() is None
    send_tool_input("call-1", "edit_file", "{")  # no sink, no error


async def test_a_native_call_that_never_returns_ends_its_previews_for_good(monkeypatch):
    from core.turn_provider_call import _StreamingCallbacks

    route = PreviewRoute()
    monkeypatch.setattr(live_preview, "previews", route)

    async def on_step(*_args):
        return None

    callbacks = _StreamingCallbacks(on_step, "thread-a")
    callbacks.on_tool_input("call-1", "write_file", '{"path": "a.md", ')
    callbacks.on_tool_input("call-2", "bash", '{"command": ')
    assert set(route.calls) == {"call-1"}
    callbacks.end_previews("stopped")
    callbacks.on_tool_input("call-1", "write_file", '"content": "x"}')
    assert route.calls == {}


async def test_a_preview_never_changes_the_document(tmp_path, monkeypatch):
    from tests.test_document_live_lane import open_lane

    live, _lane = await open_lane(tmp_path)
    frames = []
    live.preview_changed = frames.append
    entry = SimpleNamespace(live=live, policy=None)

    async def run_in_db(fn, *args):
        return fn(*args)

    monkeypatch.setattr("doceditor.live_tools.find_live", lambda path: entry)
    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)
    monkeypatch.setattr("infra.queries.get_thread", lambda thread_id: None)
    monkeypatch.setattr("tools.filesystem_gate._resolve_path", lambda path, workspace, **_: workspace / path)
    monkeypatch.setattr(live_preview, "FLUSH_SECONDS", 0)
    before = (live.text, live.doc.get_state())
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="t", tool="edit_file", workspace=tmp_path)
    route.update("call-1", json.dumps({"path": "document.md", "old_string": "quote", "new_string": "quotation"}))
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert frames and frames[0]["slots"][0]["text"] == "quotation"
    assert (live.text, live.doc.get_state()) == before


def test_the_provider_call_id_is_bound_only_inside_its_tool_run():
    from infra.state.context import _current_provider_call_id, tool_invocation_context

    with tool_invocation_context("inv-1", provider_call_id="toolu_1"):
        assert _current_provider_call_id.get() == "toolu_1"
    assert _current_provider_call_id.get() is None
    with tool_invocation_context(None, fork_id="cp", provider_call_id="toolu_1"):
        assert _current_provider_call_id.get() is None


@pytest.mark.usefixtures("isolated_db")
async def test_an_agent_write_names_the_call_its_preview_was_keyed_by(tmp_path):
    from doceditor.live_lane import Operation
    from doceditor.live_operation import TextEdit
    from doceditor.live_publication_record import WriteActor
    from infra.database import get_db
    from tests.registry_helpers import make_session
    from tests.test_document_live_lane import open_lane

    thread_id = make_session(get_db(), title="Agent Alpha")
    live, lane = await open_lane(tmp_path)
    seen = []
    live.update_changed = lambda update, sender, author=None: seen.append(author)
    await lane.submit(Operation("edit", edits=(TextEdit("quote", "quote more"),),
                                actor=WriteActor(thread_id, None, call_id="toolu_1")))
    assert seen == [{"thread_id": thread_id, "name": "Agent Alpha", "call_id": "toolu_1"}]
