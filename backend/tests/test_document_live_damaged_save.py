"""A live session saves its text once when its store is found damaged mid-session (design 5.7(a)).

The store's bytes stay as they are; the file receives the in-memory live text,
received but unacknowledged typing included, through the lane's plain writer.
"""
import asyncio
import base64
import logging

import pytest

from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

ORIGINAL = "a b\nsecond\n"


@pytest.mark.asyncio
async def test_older_owed_row_cannot_publish_over_a_damage_save(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    from doceditor.live_lane import Operation
    from doceditor.live_publication_record import WriteActor
    from tests.test_document_live_file_tools import opened
    from tests.test_document_live_damaged_fallback import _corrupt_page
    from tests.test_document_live_lane import insert
    from registry.identity_metadata import parse_metadata
    from registry import service
    from doceditor.live_document import file_hash
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    # Settle the open's drain so the older row stays owed until this case starts the publisher.
    await asyncio.gather(*(p.task for p in manager.publishers.values() if p.task is not None))
    actor = WriteActor(scope["owner"], "project-alpha")
    entry.live.viewer_actor = lambda: actor
    # Leave an older row owed without starting its publisher yet.
    entry.lane.published = None
    try:
        assert (await entry.lane.submit(Operation("replace", content="OLDER", actor=actor))).server_saved
        assert entry.live.store.next_publication().result_hash == file_hash(b"OLDER")
        insert(entry.live, "acked", "ACKED ")
        await entry.live.commit_received()
        insert(entry.live, "received", "RECEIVED ")
        damaged = _corrupt_page("live")(entry.live.store.path)
        from doceditor.live_store import StoreRefused
        with pytest.raises(StoreRefused):
            await entry.live.commit_received()
        # The callback queues the one save before this drain tries the readable table.
        await asyncio.sleep(0)
        await entry.lane._worker
        saved = entry.live.text
        assert entry.live.path.read_text() == saved
        entry.publisher.notify()
        if entry.publisher.task is not None:
            await entry.publisher.task
        artifact = service.find_file_identity(str(entry.live.path), db=scope["db"])
        assert parse_metadata(artifact["metadata"])["last_written_sha256"] == file_hash(saved.encode())
        assert entry.live.store.path.read_bytes() == damaged
    finally:
        await manager.close()



def typed(entry, base, message_id, text):
    """A browser doc_update frame that types text at the start of the current live text."""
    from doceditor.live_text import clone
    client = clone(entry.live.doc.get_update())
    vector = client.get_state()
    client["text"].insert(0, text)
    return {**base, "type": "doc_update", "message_id": message_id,
            "update": base64.b64encode(client.get_update(vector)).decode()}


def drain(socket):
    frames = []
    while not socket.sent.empty():
        frame = socket.sent.get_nowait()
        frames.append((frame["type"], frame.get("kind"), tuple(frame.get("message_ids", ()))))
    return frames


def user_of(scope):
    return {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}


async def damaged_mid_session(scope, tmp_path, monkeypatch, *, outside=None):
    """Open live, type acknowledged text, receive more, then damage the live page before its commit runs.

    ``outside`` replaces the file between the acknowledgement and the damage.
    Returns the session and the store bytes the damage left.
    """
    from ws import document_control
    from tests.test_document_live_damaged_fallback import _corrupt_page, _tools
    from tests.test_document_live_file_tools import opened
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    user, socket = user_of(scope), Socket()
    await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
                                                       "project_id": "project-alpha"})
    base = {"artifact_id": entry.artifact_id, "store_id": (await socket.next("doc_opened"))["store_id"]}
    await document_control.handle_frame(socket, user, typed(entry, base, "acked", "ACKED "))
    assert (await socket.next("doc_ack"))["message_ids"] == ["acked"]
    await _tools(scope)[0].execute(str(entry.live.path), **context)
    if outside is not None:
        entry.live.path.write_text(outside)
    schedule = document_control._schedule_commit
    monkeypatch.setattr(document_control, "_schedule_commit", lambda *args: None)
    await document_control.handle_frame(socket, user, typed(entry, base, "received", "RECEIVED "))
    # The open and the acknowledged commit started a publication drain; settle it so its next
    # read cannot land in the damage's rewrite and record a failure into the damaged bytes.
    await asyncio.gather(*(p.task for p in manager.publishers.values() if p.task is not None))
    damaged = _corrupt_page("live")(entry.live.store.path)
    monkeypatch.setattr(document_control, "_schedule_commit", schedule)
    schedule(entry.artifact_id, entry)
    await asyncio.sleep(.2)
    return {"manager": manager, "entry": entry, "context": context, "user": user, "socket": socket,
            "base": base, "damaged": damaged}


def _product_events(monkeypatch):
    from infra import product_events
    seen = []
    def emit(category, action, *, properties=None, **kwargs):
        if action.startswith("damaged_"):
            seen.append((category, action, properties))
    monkeypatch.setattr(product_events, "emit_product_event", emit)
    return seen


@pytest.mark.asyncio
async def test_damage_save_joins_an_inflight_registration_and_keeps_store_bytes(ownership_scope, tmp_path, monkeypatch):
    import threading
    from doceditor import live_publisher
    from doceditor.live_lane import Operation
    from doceditor.live_publication_record import WriteActor
    from tests.test_document_live_file_tools import opened
    from tests.test_document_live_damaged_fallback import _corrupt_page
    from tests.test_document_live_lane import insert
    from doceditor.live_document import file_hash
    from doceditor.live_store import StoreRefused
    from registry import service
    from registry.identity_metadata import parse_metadata
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    actor = WriteActor(scope["owner"], "project-alpha")
    entry.live.viewer_actor = lambda: actor
    entered, release = threading.Event(), threading.Event()
    real = live_publisher.register_publication
    def held(publication, text, result_hash, **kwargs):
        if text == "OLDER":
            entered.set()
            assert release.wait(3)
        return real(publication, text, result_hash, **kwargs)
    monkeypatch.setattr(live_publisher, "register_publication", held)
    try:
        assert (await entry.lane.submit(Operation("replace", content="OLDER", actor=actor))).server_saved
        assert await asyncio.to_thread(entered.wait, 2)
        insert(entry.live, "received", "RECEIVED ")
        damaged = _corrupt_page("live")(entry.live.store.path)
        with pytest.raises(StoreRefused):
            await entry.live.commit_received()
        await asyncio.sleep(0)
        assert entry.publisher.closed
        assert entry.live.path.read_text() == "OLDER"
        release.set()
        await entry.lane._worker
        artifact = service.find_file_identity(str(entry.live.path), db=scope["db"])
        saved = entry.live.path.read_text()
        assert saved == "RECEIVED OLDER"
        assert parse_metadata(artifact["metadata"])["last_written_sha256"] == file_hash(saved.encode())
        assert entry.live.store.path.read_bytes() == damaged
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_damage_save_never_replaces_a_file_changed_outside(ownership_scope, tmp_path, monkeypatch, caplog):
    events = _product_events(monkeypatch)
    caplog.set_level(logging.ERROR)
    session = await damaged_mid_session(ownership_scope, tmp_path, monkeypatch, outside="OUTSIDE\n")
    entry = session["entry"]
    try:
        await asyncio.sleep(.1)
        assert entry.live.path.read_text() == "OUTSIDE\n"
        assert entry.live.store.path.read_bytes() == session["damaged"]
        errors = [record for record in caplog.records
                  if record.levelno == logging.ERROR and record.name == "doceditor.live_damage"]
        assert len(errors) == 1 and "RECEIVED" not in errors[0].getMessage()
        assert events == [("documents", "damaged_save_refused",
                           {"artifact_id": entry.artifact_id, "store_id": entry.live.store.store_id,
                            "cause": "file_changed_outside"})]
    finally:
        await session["manager"].close()


@pytest.mark.asyncio
async def test_damage_save_writes_the_file_once_across_classification_points(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control
    from tests.test_document_live_damaged_fallback import _tools
    calls = []
    plain = live_runtime.write_atomic_plain
    def counted(path, text, prior=None):
        calls.append(text)
        plain(path, text, prior)
    monkeypatch.setattr(live_runtime, "write_atomic_plain", counted)
    session = await damaged_mid_session(ownership_scope, tmp_path, monkeypatch)
    entry, socket, user, base = session["entry"], session["socket"], session["user"], session["base"]
    saved = "RECEIVED ACKED " + ORIGINAL
    try:
        # The commit refusal, then a refused agent write, repeated typing and a publication pass.
        written = await _tools(ownership_scope)[1].execute(path=str(entry.live.path), content="AGENT\n",
                                                           **session["context"])
        assert written.startswith("Error")
        for number in range(3):
            await document_control.handle_frame(socket, user, typed(entry, base, f"next-{number}", "NEXT "))
        entry.publisher.notify()
        await asyncio.sleep(.3)
        assert calls == [saved]
        assert entry.live.path.read_text() == saved
        assert entry.live.store.path.read_bytes() == session["damaged"]
        assert all(frame[0] != "doc_ack" for frame in drain(socket))
        # The save is registered as the plain path registers a live write.
        import hashlib
        import json
        row = ownership_scope["db"].execute("SELECT metadata FROM artifact_identities WHERE id=?",
                                            (entry.artifact_id,)).fetchone()
        assert json.loads(row[0])["last_written_sha256"] == hashlib.sha256(saved.encode()).hexdigest()
    finally:
        await session["manager"].close()
