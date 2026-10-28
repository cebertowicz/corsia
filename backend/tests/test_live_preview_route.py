"""A streamed file-tool call is drawn on its open document once its anchor is known, in any key order."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute

DOC = "Hello world.\nSecond line \U0001F600 here.\n"


class FakeLive:
    def __init__(self, text: str):
        self.text = text
        self.store = SimpleNamespace(store_id="store-1")
        self.frames: list[dict] = []
        self.preview_changed = self.frames.append


@pytest.fixture
def doc(tmp_path, monkeypatch):
    (tmp_path / "doc.md").write_text(DOC)
    live = FakeLive(DOC)
    entry = SimpleNamespace(live=live, policy=None)  # the write gates have their own tests

    async def run_in_db(fn, *args):
        return fn(*args)

    monkeypatch.setattr("doceditor.live_tools.find_live", lambda path: entry if path.name == "doc.md" else None)
    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)
    monkeypatch.setattr("infra.queries.get_thread", lambda thread_id: {"title": "Agent Alpha", "project_id": None})
    monkeypatch.setattr("tools.filesystem_gate.resolve_path", lambda path, workspace, **_: workspace / path)
    monkeypatch.setattr(live_preview, "FLUSH_SECONDS", 0)
    return SimpleNamespace(live=live, workspace=tmp_path)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def stream(route, doc, call_id, tool, args, *, size=4, turn_id="turn-1", start=True):
    if start:
        route.start(call_id, thread_id="thread-a", turn_id=turn_id, tool=tool, workspace=doc.workspace)
    raw = json.dumps(args)
    for index in range(0, len(raw), size):
        route.update(call_id, raw[index:index + size])
        await settle()


def drawn(frames, call_id):
    """Each slot's range and the text its appended pieces add up to."""
    slots: dict[int, dict] = {}
    for frame in frames:
        if frame["type"] != "doc_preview" or frame["call_id"] != call_id:
            continue
        for part in frame["slots"]:
            slot = slots.setdefault(part["slot"], {"text": ""})
            assert part["at"] == len(slot["text"].encode("utf-16-le")) // 2
            slot["text"] += part["text"]
            if part["from"] is not None:
                slot["range"] = (part["from"], part["to"])
    return slots


EDIT_ORDERS = [
    {"path": "doc.md", "old_string": "world", "new_string": "there, friend \U0001F389"},
    {"new_string": "there, friend \U0001F389", "path": "doc.md", "old_string": "world"},
    {"old_string": "world", "new_string": "there, friend \U0001F389", "path": "doc.md"},
]


@pytest.mark.parametrize("args", EDIT_ORDERS)
async def test_edit_file_draws_its_new_text_over_the_anchor_in_any_key_order(doc, args):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "edit_file", args)
    assert drawn(doc.live.frames, "call-1") == {0: {"text": "there, friend \U0001F389", "range": (6, 11)}}
    assert {frame["name"] for frame in doc.live.frames} == {"Agent Alpha"}
    assert {frame["store_id"] for frame in doc.live.frames} == {"store-1"}


async def test_nothing_is_drawn_before_the_anchor_is_complete(doc):
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="t", tool="edit_file", workspace=doc.workspace)
    route.update("call-1", '{"path": "doc.md", "new_string": "there friend", "old_string": "wor')
    await settle()
    assert doc.live.frames == []
    route.update("call-1", 'ld"}')
    await settle()
    assert drawn(doc.live.frames, "call-1") == {0: {"text": "there friend", "range": (6, 11)}}


@pytest.mark.parametrize("anchor", ["nowhere", "e"])
async def test_an_anchor_that_never_matches_once_draws_nothing(doc, anchor):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "edit_file", {"path": "doc.md", "old_string": anchor, "new_string": "x"})
    route.end("call-1", "settled")
    assert doc.live.frames == []


@pytest.mark.parametrize("args", [
    {"path": "doc.md", "edits": [{"old_string": "Hello", "new_string": "Hi"},
                                 {"old_string": "Second", "new_string": "Third"}]},
    {"edits": [{"new_string": "Hi", "old_string": "Hello"}, {"new_string": "Third", "old_string": "Second"}],
     "path": "doc.md"},
])
async def test_multi_edit_draws_each_entry_in_its_own_slot(doc, args):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "multi_edit", args)
    assert drawn(doc.live.frames, "call-1") == {0: {"text": "Hi", "range": (0, 5)},
                                                1: {"text": "Third", "range": (13, 19)}}


@pytest.mark.parametrize("args", [
    {"path": "doc.md", "content": "Hello world.\nSecond line changed.\n"},
    {"content": "Hello world.\nSecond line changed.\n", "path": "doc.md"},
])
async def test_write_file_draws_from_its_first_change_to_the_end(doc, args):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "write_file", args)
    end = len(DOC.encode("utf-16-le")) // 2
    assert drawn(doc.live.frames, "call-1") == {0: {"text": "changed.\n", "range": (25, end)}}


async def test_interleaved_calls_each_draw_only_their_own_text(doc):
    route = PreviewRoute()
    first = json.dumps({"path": "doc.md", "old_string": "Hello", "new_string": "Howdy"})
    second = json.dumps({"path": "doc.md", "old_string": "here", "new_string": "there"})
    for call_id in ("call-1", "call-2"):
        route.start(call_id, thread_id="thread-a", turn_id="t", tool="edit_file", workspace=doc.workspace)
    for index in range(0, max(len(first), len(second)), 3):
        route.update("call-1", first[index:index + 3])
        route.update("call-2", second[index:index + 3])
        await settle()
    assert drawn(doc.live.frames, "call-1") == {0: {"text": "Howdy", "range": (0, 5)}}
    assert drawn(doc.live.frames, "call-2") == {0: {"text": "there", "range": (28, 32)}}


async def test_stop_ends_the_turns_previews_and_nothing_resurrects_them(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "edit_file", {"path": "doc.md", "old_string": "world", "new_string": "th"})
    route.end_where("stopped", turn_id="turn-1")
    assert doc.live.frames[-1] == {"store_id": "store-1", "type": "doc_preview_end", "call_id": "call-1",
                                   "thread_id": "thread-a", "outcome": "stopped"}
    count = len(doc.live.frames)
    route.update("call-1", "ere")
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool="edit_file", workspace=doc.workspace)
    route.update("call-1", '{"path": "doc.md"')
    await settle()
    assert len(doc.live.frames) == count
    assert route.calls == {}


async def test_another_turns_stop_leaves_a_call_drawing(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", "edit_file", {"path": "doc.md", "old_string": "world", "new_string": "x"})
    route.end_where("stopped", turn_id="turn-2")
    assert "call-1" in route.calls


async def test_an_input_past_the_size_cap_stops_drawing(doc, monkeypatch):
    monkeypatch.setattr(live_preview, "MAX_TEXT_BYTES", 80)
    route = PreviewRoute()
    await stream(route, doc, "call-1", "edit_file",
                 {"path": "doc.md", "old_string": "world", "new_string": "y" * 200})
    assert doc.live.frames[-1]["type"] == "doc_preview_end"
    assert doc.live.frames[-1]["outcome"] == "capped"
    assert "call-1" not in route.calls


async def test_a_call_ended_before_its_document_resolves_never_draws(doc):
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="t", tool="edit_file", workspace=doc.workspace)
    route.update("call-1", '{"path": "doc.md", "old_string": "world", "new_string": "x"}')
    route.end("call-1", "settled")
    await settle()
    assert doc.live.frames == []


async def test_a_file_that_is_not_an_open_document_is_never_drawn(doc):
    route = PreviewRoute()
    (doc.workspace / "other.md").write_text(DOC)
    await stream(route, doc, "call-1", "edit_file", {"path": "other.md", "old_string": "world", "new_string": "x"})
    assert doc.live.frames == []


def test_only_file_tools_are_previewed():
    assert live_preview.preview_tool("mcp__corsia__edit_file") == "edit_file"
    assert live_preview.preview_tool("write_file") == "write_file"
    assert live_preview.preview_tool("mcp__corsia__bash") is None
    assert live_preview.preview_tool(None) is None


def test_harness_tool_names_map_through_the_one_catalog_seam(monkeypatch):
    from tools import catalog

    monkeypatch.setattr(catalog, "MCP_CORSIA_PREFIX", "mcp__renamed__")
    assert live_preview.preview_tool("mcp__renamed__multi_edit") == "multi_edit"
    assert live_preview.preview_tool("Edit") == "edit_file"


async def test_a_parser_error_ends_only_that_preview_and_never_reaches_the_turn(doc, monkeypatch):
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="t", tool="edit_file", workspace=doc.workspace)

    def broken(self, fragment):
        raise ValueError("parser bug")

    monkeypatch.setattr(live_preview.ArgScanner, "feed", broken)
    route.update("call-1", '{"path": "doc.md"')  # must not raise into the stream pump
    assert "call-1" not in route.calls and "call-1" in route.ended
