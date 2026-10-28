"""A call made through the ``tool_call`` wrapper is previewed like the tool it names, handed over by call id."""
import json
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute
from doceditor.live_preview_args import ArgScanner
from tests.test_live_preview_comments import DOC, NAPS, doc, drawn, settle  # noqa: F401 - the `doc` fixture

WRAPPERS = ("tool_call", "mcp__corsia__tool_call")


def wrapped(name: str, arguments: dict, *, arguments_first: bool = False) -> str:
    parts = [("name", name), ("arguments", arguments)]
    return json.dumps(dict(reversed(parts) if arguments_first else parts))


async def stream(route, doc, call_id, raw, *, size=4, provider_name="tool_call"):
    route.start(call_id, thread_id="thread-a", turn_id="turn-1", tool=live_preview.preview_tool(provider_name),
                workspace=doc.workspace)
    for index in range(0, len(raw), size):
        route.update(call_id, raw[index:index + size])
        await settle()


@pytest.mark.parametrize("name", WRAPPERS)
def test_a_wrapper_name_is_previewed_and_another_tool_is_not(name):
    assert live_preview.preview_tool(name) is not None
    assert live_preview.preview_tool("mcp__corsia__bash") is None


@pytest.mark.parametrize("size", [1, 3, 7])
@pytest.mark.parametrize("name", WRAPPERS)
async def test_a_wrapped_comment_highlights_its_anchor_and_never_broadcasts_its_body(doc, name, size):
    route = PreviewRoute()
    raw = wrapped("doc_comment_create", {"doc_path": "doc.md", "quote": "naps", "body": "Should this be sleeps?"})
    await stream(route, doc, "call-1", raw, size=size, provider_name=name)
    assert drawn(doc.live.frames, "call-1") == (["comment"], NAPS, "")
    assert "sleeps" not in json.dumps(doc.live.frames)
    assert {frame["name"] for frame in doc.live.frames} == {"Agent Alpha"}


async def test_a_wrapped_suggestion_types_its_replacement_inline_and_hands_over_by_call_id(doc):
    route = PreviewRoute()
    raw = wrapped("mcp__corsia__doc_comment_create",
                  {"doc_path": "doc.md", "quote": "naps", "replacement": "dozes \U0001F634"})
    await stream(route, doc, "call-1", raw, size=3)
    kinds, span, text = drawn(doc.live.frames, "call-1")
    assert (kinds[-1], span, text) == ("suggest", NAPS, "dozes \U0001F634")
    route.end("call-1", "settled")
    ends = [frame for frame in doc.live.frames if frame["type"] == "doc_preview_end"]
    assert [(frame["call_id"], frame["outcome"]) for frame in ends] == [("call-1", "settled")]


async def test_arguments_streamed_before_the_name_draw_once_the_name_arrives(doc):
    route = PreviewRoute()
    raw = wrapped("doc_comment_create", {"doc_path": "doc.md", "quote": "naps", "body": "b"}, arguments_first=True)
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool=live_preview.preview_tool("tool_call"),
                workspace=doc.workspace)
    split = raw.index('"name"')
    route.update("call-1", raw[:split])
    await settle()
    assert drawn(doc.live.frames, "call-1") == ([], None, "")
    for index in range(split, len(raw), 5):
        route.update("call-1", raw[index:index + 5])
        await settle()
    assert drawn(doc.live.frames, "call-1") == (["comment"], NAPS, "")


async def test_a_name_split_across_fragments_is_read_whole(doc):
    route = PreviewRoute()
    raw = wrapped("doc_comment_create", {"doc_path": "doc.md", "quote": "naps", "body": "b"})
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool=live_preview.preview_tool("tool_call"),
                workspace=doc.workspace)
    cut = raw.index("comment_cre") + 4  # inside the tool's name
    for piece in (raw[:cut], raw[cut:cut + 4], raw[cut + 4:]):
        route.update("call-1", piece)
        await settle()
    assert drawn(doc.live.frames, "call-1") == (["comment"], NAPS, "")


async def test_a_wrapped_tool_that_is_not_previewed_draws_nothing_and_ends_the_call(doc):
    route = PreviewRoute()
    raw = wrapped("bash", {"command": "echo naps", "path": "doc.md", "quote": "naps", "body": "b"})
    await stream(route, doc, "call-1", raw, size=5)
    assert doc.live.frames == []
    assert "call-1" in route.ended


async def test_a_wrapper_around_a_wrapper_is_not_previewed(doc):
    route = PreviewRoute()
    inner = {"name": "doc_comment_create", "arguments": {"doc_path": "doc.md", "quote": "naps", "body": "b"}}
    await stream(route, doc, "call-1", wrapped("tool_call", inner), size=5)
    assert doc.live.frames == []


async def test_a_wrapped_file_edit_resolves_its_workspace_from_the_real_tool(doc, monkeypatch):
    seen = []
    monkeypatch.setattr("doceditor.live_tools.find_live", lambda path: SimpleNamespace(live=doc.live, policy=None))
    monkeypatch.setattr("tools.filesystem_gate.resolve_path",
                        lambda path, workspace, **_: seen.append(workspace) or workspace / path)
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool=live_preview.preview_tool("tool_call"),
                workspace=None, workspace_for=lambda tool: doc.workspace if tool == "edit_file" else None)
    route.update("call-1", wrapped("edit_file", {"path": "doc.md", "old_string": "naps", "new_string": "sleeps"}))
    await settle()
    assert seen == [doc.workspace]
    assert {frame["kind"] for frame in doc.live.frames} == {"edit"}


def test_the_scanner_reads_the_wrappers_name_apart_from_the_nested_arguments():
    scanner = ArgScanner(nested="arguments")
    raw = json.dumps({"arguments": {"name": "inner", "edits": [{"old_string": "a", "new_string": "b"}], "path": "x"},
                      "name": "edit_file"})
    for index in range(0, len(raw), 3):
        scanner.feed(raw[index:index + 3])
    assert not scanner.failed
    assert scanner.outer["name"].text() == "edit_file" and scanner.outer["name"].complete
    assert scanner.string("name").text() == "inner" and scanner.string("path").text() == "x"
    assert [(e["old_string"].text(), e["new_string"].text()) for e in scanner.edits] == [("a", "b")]


async def test_the_native_provider_sink_feeds_a_wrapped_call_to_the_route(monkeypatch):
    from core.turn_provider_call import _StreamingCallbacks

    started = []
    route = SimpleNamespace(start=lambda call_id, **kw: started.append((call_id, kw["tool"])),
                            update=lambda *_: None)
    monkeypatch.setattr("doceditor.live_preview.previews", route)
    callbacks = _StreamingCallbacks(None, "thread-a")
    callbacks.on_tool_input("call-1", "tool_call", '{"name": "doc_')
    callbacks.on_tool_input("call-2", "bash", "{}")
    assert started == [("call-1", live_preview.preview_tool("tool_call"))]


async def test_claude_code_starts_a_wrapped_call_with_the_real_tools_workspace(monkeypatch):
    from subagents import cc_event_preview
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    started = []
    route = SimpleNamespace(start=lambda call_id, **kw: started.append((call_id, kw)), update=lambda *_: None)
    monkeypatch.setattr(cc_event_preview, "previews", route)
    live = _Live(session=None, parent_thread_id=None, name="agent")
    live.tool_registry = SimpleNamespace(get=lambda name: SimpleNamespace(workspace=f"ws-of-{name}"))
    live.corsia_turn = SimpleNamespace(turn_id="turn-1")
    ctx = CCEventContext(live=live, child_id="thread-a", bcast=lambda *_: None, db=None)
    await cc_event_preview.on_tool_input_start(ctx, {"index": 0, "call_id": "toolu_1", "name": "mcp__corsia__tool_call"})
    (call_id, kwargs), = started
    assert call_id == "toolu_1" and kwargs["tool"] == live_preview.preview_tool("tool_call")
    assert kwargs["workspace_for"]("doc_comment_create") == "ws-of-doc_comment_create"
