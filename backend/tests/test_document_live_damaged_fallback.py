"""A damaged store's document stays editable without live editing (design 5.7a, revision 16).

The store's bytes are compared before and after every case: nothing renames,
rewrites or deletes them.
"""
import asyncio
from contextlib import closing
import hashlib
import logging
import os
import sqlite3

import pytest

from doceditor.live_admission import OpenRefused
from doceditor.live_store import LiveStore, PendingWrite, StoreRefused
from tests.test_document_live_legacy_save import old_client, save_old
from tests.test_document_live_refused_store import _closed, _damage, _events, _store_path
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

ORIGINAL = "a b\nsecond\n"


def _corrupt_page(table):
    """Overwrite one table's root page; the schema and meta pages stay readable."""
    def damage(store):
        with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as db:
            size = db.execute("PRAGMA page_size").fetchone()[0]
            root = db.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        data = bytearray(store.read_bytes())
        data[(root - 1) * size:root * size] = b"\xa5" * size
        store.write_bytes(bytes(data))
        return store.read_bytes()
    return damage


DAMAGE = [_damage, _corrupt_page("live"), _corrupt_page("mirror")]
DAMAGE_IDS = ["not_a_database", "live_page", "mirror_page"]


async def _closed_with_history(scope, tmp_path, monkeypatch):
    """A closed document whose store holds acknowledged typing the file does not."""
    from tests.test_document_live_file_tools import opened
    from tests.test_document_live_suggestions import typing
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    typing(entry, 0, insert="ACK ")
    await entry.live.commit_received()
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    # Eviction leaves the publication drain its commits started running; settle it so
    # nothing of the closed session can still read or write the store the case damages.
    await asyncio.gather(*(p.task for p in manager.publishers.values() if p.task is not None))
    assert entry.live.path.read_text() == ORIGINAL
    return manager, entry.artifact_id, entry.live.path, context


def _refuses_after_meta(store, damage):
    """Run LiveStore.__init__ (schema script and meta insert) and every read on these bytes."""
    try:
        reopened = LiveStore(store)
    except StoreRefused:
        assert damage is _damage, "Page damage must be found after the schema and meta read"
        return
    with pytest.raises(StoreRefused):
        reopened.load_updates()
        reopened.mirror()


def _tools(scope):
    from tools.filesystem_edit import EditFileTool
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    workspace = scope["workspace"]
    return ReadFileTool(workspace=workspace), WriteFileTool(workspace=workspace), EditFileTool(workspace=workspace)


def _user(scope):
    return {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", DAMAGE, ids=DAMAGE_IDS)
async def test_damaged_store_takes_legacy_save_and_agent_writes_through_the_plain_writer(
        ownership_scope, tmp_path, monkeypatch, damage):
    scope = ownership_scope
    manager, artifact_id, path, context = await _closed_with_history(scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    before = damage(store)
    reader, writer, editor = _tools(scope)
    try:
        with pytest.raises(OpenRefused) as refused:
            await manager.open(artifact_id, path)
        assert refused.value.kind == "store_damaged"
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, path, "LEGACY\n")
        assert response.status_code == 200 and path.read_text() == "LEGACY\n"
        assert store.read_bytes() == before
        await reader.execute(str(path), **context)
        written = await writer.execute(path=str(path), content="AGENT\n", **context)
        assert not written.startswith("Error") and path.read_text() == "AGENT\n"
        assert store.read_bytes() == before
        edited = await editor.execute(str(path), old_string="AGENT", new_string="EDITED", **context)
        assert not edited.startswith("Error") and path.read_text() == "EDITED\n"
        assert store.read_bytes() == before
        assert manager.refused_stores[path]["code"] == "store_damaged"
        assert manager.documents == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", DAMAGE, ids=DAMAGE_IDS)
async def test_ws_open_of_a_damaged_store_answers_non_live_with_the_reason(
        ownership_scope, tmp_path, monkeypatch, caplog, damage):
    from ws import document_control
    scope = ownership_scope
    manager, artifact_id, path, _ = await _closed_with_history(scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    before, listing = damage(store), sorted(os.listdir(manager.root))
    events = _events(monkeypatch)
    socket = Socket()
    frame = {"type": "doc_open", "path": str(path), "project_id": "project-alpha", "request_id": "r1"}
    try:
        with caplog.at_level(logging.ERROR):
            await document_control.handle_frame(socket, _user(scope), frame)
            await document_control.handle_frame(socket, _user(scope), {**frame, "request_id": "r2"})
        for request_id in ("r1", "r2"):
            opened = await socket.next("doc_opened")
            assert opened == {"type": "doc_opened", "live": False, "live_unavailable": "store_damaged",
                              "request_id": request_id}
        assert [action for _, action, _ in events] == ["store_damaged", "store_damaged"]
        assert sum(record.levelno == logging.ERROR for record in caplog.records) >= 2
        assert store.read_bytes() == before and sorted(os.listdir(manager.root)) == listing
        assert manager.documents == {}
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_ws_open_of_an_unavailable_store_is_still_refused(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    scope = ownership_scope
    manager, artifact_id, path, _ = await _closed(scope, tmp_path, monkeypatch)
    holder = sqlite3.connect(_store_path(manager, artifact_id), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    socket = Socket()
    try:
        await document_control.handle_frame(socket, _user(scope), {"type": "doc_open", "path": str(path),
                                                                   "project_id": "project-alpha"})
        refused = await socket.next("doc_error")
        assert refused["kind"] == "store_unavailable" and refused["blocking"]
    finally:
        holder.rollback()
        holder.close()
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", DAMAGE, ids=DAMAGE_IDS)
async def test_restart_refuses_the_same_damaged_bytes_with_the_same_kind(ownership_scope, tmp_path, monkeypatch, damage):
    from doceditor.live_runtime import LiveDocuments
    manager, artifact_id, path, _ = await _closed_with_history(ownership_scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    before = damage(store)
    listing = sorted(os.listdir(manager.root))
    with pytest.raises(OpenRefused) as first:
        await manager.open(artifact_id, path)
    await manager.close()
    restarted = LiveDocuments(manager.root, workspace=manager.workspace, db_path=manager.db_path)
    await restarted.startup()
    with pytest.raises(OpenRefused) as second:
        await restarted.open(artifact_id, path)
    await restarted.close()
    _refuses_after_meta(store, damage)
    assert first.value.kind == second.value.kind == "store_damaged"
    assert store.read_bytes() == before
    assert sorted(os.listdir(manager.root)) == listing


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", DAMAGE[1:], ids=DAMAGE_IDS[1:])
async def test_closed_session_drain_failing_late_never_writes_into_the_damaged_store(
        ownership_scope, tmp_path, monkeypatch, damage):
    """The closed session's publication drain is still reading when the store is damaged.

    Its one slow read fails as a store that may clear (a torn read of a store
    being rewritten). Recording that failure is a write, and it must have landed
    before the damage, not into the damaged bytes after it.
    """
    import time
    real, failed = LiveStore.next_publication, []

    def slow_then_fail(self):
        if not failed:
            failed.append(True)
            time.sleep(.3)
            raise StoreRefused("disk I/O error")
        return real(self)
    monkeypatch.setattr(LiveStore, "next_publication", slow_then_fail)
    manager, artifact_id, path, _ = await _closed_with_history(ownership_scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    before = damage(store)
    # The open's refusal would stop the drain first, so let it fail before the open.
    await asyncio.gather(*(p.task for p in manager.publishers.values() if p.task is not None))
    with pytest.raises(OpenRefused):
        await manager.open(artifact_id, path)
    await manager.close()
    assert failed and store.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [_damage, _corrupt_page("pending"), _corrupt_page("mirror")],
                         ids=["not_a_database", "pending_page", "mirror_page"])
async def test_store_damaged_while_live_keeps_the_lane_refusing_with_no_plain_fallback(
        ownership_scope, tmp_path, monkeypatch, damage):
    from tests.test_document_live_file_tools import opened
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = entry.live.path
    reader, writer, _ = _tools(scope)
    await reader.execute(str(path), **context)
    # The open started a publication drain; settle it so its next read cannot land in the
    # damage's rewrite and record a failure into the bytes this case snapshots.
    await asyncio.gather(*(p.task for p in manager.publishers.values() if p.task is not None))
    before = damage(entry.live.store.path)
    try:
        # The write stays in the live lane: the store refuses its barrier, so nothing reaches the file.
        written = await writer.execute(path=str(path), content="AGENT\n", **context)
        assert written.startswith("Error")
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, path, "LEGACY\n")
        assert response.status_code == 409 and response.json()["detail"]["code"] == "document_live"
        assert path.read_text() == ORIGINAL
        assert entry.live.store.path.read_bytes() == before
        assert manager.documents
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_later_open_keeps_non_live_edits_and_acknowledged_history(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_runtime import LiveDocuments
    scope = ownership_scope
    manager, artifact_id, path, _ = await _closed_with_history(scope, tmp_path, monkeypatch)
    holder = sqlite3.connect(_store_path(manager, artifact_id), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(OpenRefused):
            await manager.open(artifact_id, path)
    finally:
        holder.rollback()
        holder.close()
    await manager.close()
    # After a restart the in-process record is empty, so a plain write reaches the file.
    restarted = LiveDocuments(manager.root, workspace=manager.workspace, db_path=manager.db_path)
    from doceditor import live_runtime
    monkeypatch.setattr(live_runtime, "_manager", restarted)
    async with old_client(scope, monkeypatch) as client:
        assert (await save_old(client, path, ORIGINAL + "NONLIVE\n")).status_code == 200
    try:
        reopened = await restarted.open(artifact_id, path)
        assert reopened.live.text == "ACK " + ORIGINAL + "NONLIVE\n"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_later_open_holds_a_pending_row_the_non_live_write_moved_past(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_runtime import LiveDocuments
    from doceditor.live_text import create
    from tests.test_document_live_file_tools import opened
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    artifact_id, path = entry.artifact_id, entry.live.path
    await manager.evict(artifact_id, entry, can_evict=lambda: True)
    await manager.close()
    store = LiveStore(_store_path(manager, artifact_id))
    candidate = create("CANDIDATE\n").get_update()
    store.record_pending(PendingWrite("crashed", candidate, candidate, store.mirror().file_hash,
                                      hashlib.sha256(b"CANDIDATE\n").hexdigest()))
    restarted = LiveDocuments(manager.root, workspace=manager.workspace, db_path=manager.db_path)
    from doceditor import live_runtime
    monkeypatch.setattr(live_runtime, "_manager", restarted)
    async with old_client(scope, monkeypatch) as client:
        assert (await save_old(client, path, "NONLIVE\n")).status_code == 200
    try:
        await restarted.open(artifact_id, path)
        pending = store.pending()
        assert pending.state == "held" and pending.outside_hash == hashlib.sha256(b"NONLIVE\n").hexdigest()
        assert path.read_text() == "NONLIVE\n"
    finally:
        await restarted.close()



@pytest.mark.asyncio
async def test_known_limit_5_live_page_damaged_mid_session(ownership_scope, tmp_path, monkeypatch):
    """Design 5.7(a) known limit (5): the live table page is damaged while the document is open live.

    The first damage found mid-session saves the live text to the file once,
    acknowledged and received typing included. The agent write stays refused,
    later browser messages are received but never acknowledged, and the reopen
    is non-live with the saved text. What remains lost from the server is only
    typing that arrives after that save; the authoring device keeps it.
    """
    import asyncio
    from doceditor import live_runtime
    from doceditor.live_runtime import LiveDocuments
    from ws import document_control
    from tests.test_document_live_damaged_save import damaged_mid_session, drain, typed
    session = await damaged_mid_session(ownership_scope, tmp_path, monkeypatch)
    manager, entry, user, socket = session["manager"], session["entry"], session["user"], session["socket"]
    path, store, damaged, saved = entry.live.path, entry.live.store.path, session["damaged"], "RECEIVED ACKED " + ORIGINAL
    try:
        # The damage save holds the acknowledged and the received text; the store is untouched.
        assert path.read_text() == saved and store.read_bytes() == damaged
        # The agent write stays refused and records nothing.
        written = await _tools(ownership_scope)[1].execute(path=str(path), content=entry.live.text + "AGENT\n",
                                                           **session["context"])
        assert written == "Error writing file: database disk image is malformed"
        assert path.read_text() == saved and entry.live.store.pending() is None
        # The next typing is received in memory, then refused; nothing after the damage is acked.
        await document_control.handle_frame(socket, user, typed(entry, session["base"], "next", "NEXT "))
        await asyncio.sleep(.2)
        assert drain(socket) == [("doc_received", None, ("received",)), ("doc_error", "store_refused", ()),
                                 ("doc_received", None, ("next",)), ("doc_error", "store_refused", ())]
        assert list(entry.live.received) == ["received", "next"] and store.read_bytes() == damaged
        assert entry.live.text == "NEXT " + saved
        # Close and restart: the reopen is non-live and shows the saved text.
        await document_control.disconnect(socket)
        await manager.close()
        restarted = LiveDocuments(manager.root, workspace=manager.workspace, db_path=manager.db_path)
        monkeypatch.setattr(live_runtime, "_manager", restarted)
        await restarted.startup()
        again = Socket()
        await document_control.handle_frame(again, user, {"type": "doc_open", "path": str(path),
                                                          "project_id": "project-alpha", "request_id": "r"})
        assert await again.next("doc_opened") == {"type": "doc_opened", "live": False,
                                                  "live_unavailable": "store_damaged", "request_id": "r"}
        await restarted.close()
        assert store.read_bytes() == damaged and path.read_text() == saved
        with pytest.raises(StoreRefused):
            LiveStore(store).load_updates()
    finally:
        await manager.close()
