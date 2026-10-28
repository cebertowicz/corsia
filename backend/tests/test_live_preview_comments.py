"""A streamed comment highlights its anchor and a streamed suggestion types its insert, under the tool's own gate."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute

DOC = "Intro line.\nThe fox \U0001F98A jumps. The fox naps.\nLast line.\n"
TOOL = "doc_comment_create"


class FakeLive:
    def __init__(self, text: str):
        self.text = text
        self.ranges: dict[str, tuple[int, int]] = {"parent-1": (0, 5)}
        self.store = SimpleNamespace(store_id="store-1")
        self.frames: list[dict] = []
        self.preview_changed = self.frames.append


@pytest.fixture
def doc(tmp_path, monkeypatch):
    live = FakeLive(DOC)
    # The owner-only write rule refuses everyone: a comment or suggestion must not consult it.
    policy = SimpleNamespace(admits=lambda actor: (_ for _ in ()).throw(AssertionError("owner-only rule consulted")))
    entry = SimpleNamespace(live=live, policy=policy, artifact_id="art-1")
    seen: list[dict] = []

    async def run_in_db(fn, *args):
        return fn(*args)

    def resolve_target(db, actor_thread_id, actor, parent_id, kwargs):
        seen.append({"thread": actor_thread_id, "user": actor["user_id"], "parent_id": parent_id, **kwargs})
        if "denied" in (kwargs.get("doc_path") or kwargs.get("artifact_id") or parent_id or ""):
            raise PermissionError("Artifact not found")
        return "art-1", str(tmp_path / "doc.md"), None

    # Import before get_db is patched, so no module first imported here binds the stand-in for good.
    import doceditor.live_tools  # noqa: F401
    import tools.documents  # noqa: F401

    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)
    monkeypatch.setattr("infra.database.get_db", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("infra.queries.get_thread",
                        lambda thread_id, db=None: {"title": "Agent Alpha", "project_id": None, "user_id": "user-1"})
    monkeypatch.setattr("tools.documents._resolve_comment_target", resolve_target)
    monkeypatch.setattr("doceditor.live_tools.find_live_artifact", lambda aid: entry if aid == "art-1" else None)
    monkeypatch.setattr(live_preview, "FLUSH_SECONDS", 0)
    return SimpleNamespace(live=live, workspace=tmp_path, seen=seen)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def stream(route, doc, call_id, args, *, size=4):
    route.start(call_id, thread_id="thread-a", turn_id="turn-1", tool=TOOL, workspace=doc.workspace)
    raw = json.dumps(args)
    for index in range(0, len(raw), size):
        route.update(call_id, raw[index:index + size])
        await settle()


def drawn(frames, call_id):
    """The call's kinds in order, its last range, and the text its appended pieces add up to."""
    kinds, text, span = [], "", None
    for frame in frames:
        if frame["type"] != "doc_preview" or frame["call_id"] != call_id:
            continue
        if not kinds or kinds[-1] != frame["kind"]:
            kinds.append(frame["kind"])
        for part in frame["slots"]:
            assert part["slot"] == 0
            assert part["at"] == len(text.encode("utf-16-le")) // 2
            text += part["text"]
            if part["from"] is not None:
                span = (part["from"], part["to"])
    return kinds, span, text


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


NAPS = (u16(DOC[:DOC.index("naps")]), u16(DOC[:DOC.index("naps")]) + 4)


@pytest.mark.parametrize("args", [
    {"doc_path": "doc.md", "quote": "naps", "body": "Should this be sleeps?"},
    {"body": "Should this be sleeps?", "quote": "naps", "doc_path": "doc.md"},
    {"quote": "naps", "body": "Should this be sleeps?", "artifact_id": "art-1"},
])
async def test_a_comment_highlights_its_anchor_and_never_broadcasts_its_body(doc, args):
    route = PreviewRoute()
    await stream(route, doc, "call-1", args)
    assert drawn(doc.live.frames, "call-1") == (["comment"], NAPS, "")
    assert "sleeps" not in json.dumps(doc.live.frames)
    assert {frame["name"] for frame in doc.live.frames} == {"Agent Alpha"}


async def test_a_suggestion_types_its_replacement_inline_at_its_anchor(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "doc.md", "quote": "naps", "replacement": "dozes \U0001F634"})
    kinds, span, text = drawn(doc.live.frames, "call-1")
    assert (kinds[-1], span, text) == ("suggest", NAPS, "dozes \U0001F634")


async def test_a_suggestion_typed_after_its_quote_turns_the_highlight_into_a_suggestion(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "doc.md", "quote": "naps", "body": "tighter", "replacement": "dozes"})
    assert drawn(doc.live.frames, "call-1") == (["comment", "suggest"], NAPS, "dozes")


@pytest.mark.parametrize("args", [
    {"replacement": "dozes", "quote": "naps", "doc_path": "doc.md"},
    {"quote": "naps", "replacement": "dozes", "artifact_id": "art-1"},
    {"replacement": "dozes", "doc_path": "doc.md", "quote": "naps"},
])
async def test_a_suggestion_is_drawn_whole_whatever_order_the_keys_arrive_in(doc, args):
    route = PreviewRoute()
    await stream(route, doc, "call-1", args)
    assert drawn(doc.live.frames, "call-1") == (["suggest"], NAPS, "dozes")


async def test_a_suggestion_whose_target_arrives_last_is_drawn_whole_and_ends_settled(doc):
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool=TOOL, workspace=doc.workspace)
    route.update("call-1", json.dumps({"quote": "naps", "replacement": "dozes off", "doc_path": "doc.md"}))
    await settle()
    route.end("call-1", "settled")
    assert [frame["type"] for frame in doc.live.frames] == ["doc_preview", "doc_preview_end"]
    assert drawn(doc.live.frames, "call-1") == (["suggest"], NAPS, "dozes off")
    assert doc.live.frames[-1]["outcome"] == "settled"


async def test_a_repeated_quote_waits_for_near_before_anything_is_drawn(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "doc.md", "quote": "The fox", "near": "naps", "body": "b"})
    start = u16(DOC[:DOC.index("The fox naps")])
    assert drawn(doc.live.frames, "call-1") == (["comment"], (start, start + 7), "")
    route2 = PreviewRoute()
    await stream(route2, doc, "call-2", {"doc_path": "doc.md", "quote": "The fox", "body": "b"})
    assert drawn(doc.live.frames, "call-2") == ([], None, "")


async def test_a_reply_highlights_its_parent_anchor(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"parent_id": "parent-1", "body": "Agreed."})
    assert drawn(doc.live.frames, "call-1") == (["comment"], (0, 5), "")
    assert doc.seen[0]["parent_id"] == "parent-1"


async def test_a_whole_document_review_draws_nothing(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "doc.md", "review": True, "body": "Looks good."})
    assert drawn(doc.live.frames, "call-1") == ([], None, "")


async def test_the_preview_applies_the_tools_own_gate_as_the_thread_owner(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "notes/doc.md", "quote": "naps", "body": "b"})
    assert doc.seen[0]["user"] == "user-1" and doc.seen[0]["thread"] == "thread-a"
    assert doc.seen[0]["doc_path"] == str(doc.workspace / "notes" / "doc.md")


async def test_a_target_the_tool_would_refuse_is_never_previewed(doc):
    route = PreviewRoute()
    await stream(route, doc, "call-1", {"doc_path": "denied.md", "quote": "naps", "replacement": "dozes"})
    assert drawn(doc.live.frames, "call-1") == ([], None, "")
    assert "call-1" in route.ended


async def test_an_edit_frame_names_its_kind(doc, monkeypatch):
    monkeypatch.setattr("doceditor.live_tools.find_live", lambda path: SimpleNamespace(live=doc.live, policy=None))
    monkeypatch.setattr("tools.filesystem_gate.resolve_path", lambda path, workspace, **_: workspace / path)
    route = PreviewRoute()
    route.start("call-1", thread_id="thread-a", turn_id="turn-1", tool="edit_file", workspace=doc.workspace)
    route.update("call-1", json.dumps({"path": "doc.md", "old_string": "naps", "new_string": "sleeps"}))
    await settle()
    assert {frame["kind"] for frame in doc.live.frames} == {"edit"}


def test_doc_comment_create_is_previewed_under_any_harness_name():
    assert live_preview.preview_tool("doc_comment_create") == TOOL
    assert live_preview.preview_tool("mcp__corsia__doc_comment_create") == TOOL
