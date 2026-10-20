"""Server comment positions describe one UTF-16 live snapshot."""
import pytest

from doceditor import comment_anchoring
from doceditor.doc_text import read_doc_text
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_text import text_digest
import asyncio
import base64

from tests.test_document_live_transport import Socket
from tests.test_document_live_suggestions import suggestion, typing
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
async def test_comment_reload_uses_live_text_without_persisting_unsaved_offsets(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    typing(entry, 0, insert="\U0001f680 ")
    try:
        assert read_doc_text(str(entry.live.path)) == "\U0001f680 a b\nsecond\n"
        threads, checked = comment_anchoring.anchored_threads(row["artifact_id"], str(entry.live.path), scope["db"])
        assert checked and threads[0]["from_off"] == 5 and threads[0]["to_off"] == 6
        stored = scope["db"].execute("SELECT from_off,to_off FROM doc_comments WHERE id=?", (row["id"],)).fetchone()
        assert tuple(stored) == (2, 3)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_publication_positions_exclude_typing_after_candidate_snapshot(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, hold)
    typing(entry, 0, insert="SAVED ")
    call = asyncio.create_task(entry.lane.submit(Operation("flush", actor=WriteActor(scope["owner"], "project-alpha"))))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        typing(entry, 0, insert="LATER ")
        release.set()
        assert (await call).server_saved
        async def published():
            while await asyncio.to_thread(entry.live.store.next_publication):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(published(), 3)
        stored = scope["db"].execute("SELECT from_off,to_off FROM doc_comments WHERE id=?", (row["id"],)).fetchone()
        assert tuple(stored) == (8, 9)
        assert entry.live.ranges[row["id"]] == (14, 15)
        assert entry.live.path.read_text() == "SAVED a b\nsecond\n"
        assert entry.live.text == "LATER SAVED a b\nsecond\n"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_position_frames_and_publication_offsets_share_utf16_snapshot(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    actor = WriteActor(scope["owner"], "project-alpha", direct_user=True)
    document_control._subscriptions[socket] = {entry.artifact_id: document_control.Subscription(entry, actor, True, user["id"])}
    base = {"type": "doc_positions", "artifact_id": entry.artifact_id, "store_id": entry.live.store.store_id}
    try:
        # Exclusive boundaries keep insertions beside the quote outside it.
        typing(entry, 2, insert="\U0001f680 ")
        typing(entry, 8, insert="!")  # UTF-8 byte index immediately after b.
        assert entry.live.range_text(row["id"]) == "b"
        await document_control.handle_frame(socket, user, {**base, "seq": 1})
        frame = await socket.next("doc_positions")
        snapshot = entry.live.text
        assert frame["seq"] == 1 and frame["store_id"] == base["store_id"]
        assert frame["digest"] == text_digest(snapshot)
        assert base64.b64decode(frame["vector"]) == entry.live.doc.get_state()
        span = next(value for value in frame["ranges"] if value["id"] == row["id"])
        assert snapshot.encode("utf-16-le")[span["from_off"] * 2:span["to_off"] * 2].decode("utf-16-le") == "b"
        typing(entry, 0, insert="PREFIX ")
        await document_control.handle_frame(socket, user, {**base, "seq": 2})
        changed = await socket.next("doc_positions")
        assert changed["seq"] == 2 and changed["digest"] != frame["digest"]
        assert changed["ranges"][0]["from_off"] == span["from_off"] + 7
        await entry.lane.submit(Operation("flush", actor=actor))
        async def published():
            while await asyncio.to_thread(entry.live.store.next_publication):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(published(), 3)
        stored = scope["db"].execute("SELECT from_off,to_off FROM doc_comments WHERE id=?", (row["id"],)).fetchone()
        assert tuple(stored) == (changed["ranges"][0]["from_off"], changed["ranges"][0]["to_off"])
        document_control._subscriptions.pop(socket)
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        restored = await manager.open(entry.artifact_id, entry.live.path)
        assert restored.live.ranges[row["id"]] == tuple(stored)
        assert restored.live.range_text(row["id"]) == "b"
    finally:
        document_control._subscriptions.pop(socket, None)
        await manager.close()


def _duplicate_quotes(scope, entry):
    """The live comment e2e's document: two comments on one repeated quote and one on unique text."""
    from doceditor import comments
    from doceditor.quote_anchor import anchor_context
    quote = "duplicate quote S"
    body = f"# Comments S\n\n{quote}\n\n{quote}\n\nreply anchor S\n"
    typing(entry, 0, delete=len(entry.live.text), insert=body)
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(entry.live.path),)).fetchone()[0]
    rows, start = [], -1
    for anchor in (quote, quote, "reply anchor S"):
        start = body.find(anchor, start + 1)
        prefix, suffix = anchor_context(body, start, start + len(anchor), anchor)
        row, _ = comments.create_with_status(artifact, body=f"on {anchor}", author="author", author_kind="agent",
            author_thread_id=scope["owner"], anchor_text=anchor, from_off=start, to_off=start + len(anchor),
            anchor_prefix=prefix, anchor_suffix=suffix, db=scope["db"])
        entry.live.track_range(row["id"], row["from_off"], row["to_off"])
        rows.append(row)
    return quote, rows


def _edit(entry, start, delete, insert=""):
    """One browser edit; a deletion leaves the state vector unchanged, so `typing`'s vector-named id would repeat."""
    from doceditor.live_store import BrowserMessage
    from doceditor.live_text import clone
    client = clone(entry.live.doc.get_update())
    vector = client.get_state()
    del client["text"][start:start + delete]
    if insert:
        client["text"].insert(start, insert)
    entry.live.receive(BrowserMessage(f"edit-{start}-{delete}-{len(entry.live.text)}-{vector.hex()}", client.get_update(vector)))


def _delete_first_line(entry, text):
    """The e2e's deleteRenderedLine: the first line holding `text`, with its preceding line break."""
    _edit(entry, entry.live.text.index(text) - 1, len(text) + 1)


@pytest.mark.asyncio
async def test_reload_frame_names_only_ranges_its_resolution_placed(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    quote, (first, second, reply) = _duplicate_quotes(scope, entry)
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    actor = WriteActor(scope["owner"], "project-alpha", direct_user=True)
    document_control._subscriptions[socket] = {entry.artifact_id: document_control.Subscription(entry, actor, True, user["id"])}
    base = {"type": "doc_positions", "artifact_id": entry.artifact_id, "store_id": entry.live.store.store_id}
    try:
        typing(entry, len(entry.live.text), insert="remote edit S")
        _delete_first_line(entry, quote)
        _delete_first_line(entry, quote)
        assert quote not in entry.live.text
        # The deleted quotes' live ranges collapsed; the server still holds them.
        assert entry.live.ranges[first["id"]][0] == entry.live.ranges[first["id"]][1]
        assert entry.live.ranges[second["id"]][0] == entry.live.ranges[second["id"]][1]

        # Typed back after other text: neither occurrence keeps either comment's context, so the
        # resolver leaves both orphaned, and the reload answer must not report a place for them.
        typing(entry, len(entry.live.text), insert=f"\n{quote}\n{quote}")
        await document_control.handle_frame(socket, user, {**base, "seq": 1, "reload": True})
        frame = await socket.next("doc_positions")
        snapshot = entry.live.text.encode("utf-16-le")
        named = {span["id"]: snapshot[span["from_off"] * 2:span["to_off"] * 2].decode("utf-16-le")
                 for span in frame["ranges"]}
        assert named == {reply["id"]: "reply anchor S"}

        # Restored where it was, each comment's context names one distinct occurrence again.
        appended = f"\n{quote}\n{quote}"
        _edit(entry, len(entry.live.text) - len(appended), len(appended))
        _edit(entry, entry.live.text.index("\n\n\n\nreply"), 4, f"\n\n{quote}\n\n{quote}\n\n")
        await document_control.handle_frame(socket, user, {**base, "seq": 2, "reload": True})
        restored = await socket.next("doc_positions")
        spans = {span["id"]: (span["from_off"], span["to_off"]) for span in restored["ranges"]}
        snapshot = entry.live.text.encode("utf-16-le")
        assert {key: snapshot[a * 2:b * 2].decode("utf-16-le") for key, (a, b) in spans.items()} == {
            first["id"]: quote, second["id"]: quote, reply["id"]: "reply anchor S"}
        assert spans[first["id"]] < spans[second["id"]]
    finally:
        document_control._subscriptions.pop(socket, None)
        await manager.close()
