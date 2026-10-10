"""Pending rows drive one truthful state on open, save and settlement frames."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import StoreRefused
from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control


@asynccontextmanager
async def transport(scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    sockets = []

    async def connect():
        socket = Socket()
        sockets.append(socket)
        await document_control.handle_frame(socket, user, {
            "type": "doc_open", "path": str(entry.live.path), "project_id": "project-alpha",
            "thread_id": scope["owner"], "request_id": "open"})
        return socket, await socket.next("doc_opened")

    try:
        yield entry, user, connect
    finally:
        for socket in sockets:
            await document_control.disconnect(socket)
        await manager.close()


async def refuse_promotion(entry, monkeypatch, scope):
    promote = entry.live.store.promote
    def refused(*args):
        raise StoreRefused("promotion is unavailable")
    monkeypatch.setattr(entry.live.store, "promote", refused)
    actor = WriteActor(scope["owner"], "project-alpha")
    result = await entry.lane.submit(Operation("replace", content="candidate", actor=actor))
    assert result.outcome == "landed" and not result.server_saved
    assert entry.live.store.pending() is not None
    if entry.lane.retry.timer:
        entry.lane.retry.timer.cancel()
        entry.lane.retry.timer = None
    return promote


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["none", "pending", "held"])
async def test_open_reports_pending_only_for_nonheld_row(ownership_scope, tmp_path, monkeypatch, state):
    async with transport(ownership_scope, tmp_path, monkeypatch) as (entry, user, connect):
        if state != "none":
            promote = await refuse_promotion(entry, monkeypatch, ownership_scope)
            if state == "held":
                entry.live.path.write_text("outside")
                row = entry.live.store.pending()
                from doceditor.live_document import file_hash
                await asyncio.to_thread(entry.live.store.mark_held, row.op_id, file_hash(b"outside"))
            monkeypatch.setattr(entry.live.store, "promote", promote)
        _, frame = await connect()
        assert frame["pending"] is (state == "pending")
        assert (frame["held"] is not None) is (state == "held")


@pytest.mark.asyncio
async def test_saved_after_refused_promote_reports_pending(ownership_scope, tmp_path, monkeypatch):
    async with transport(ownership_scope, tmp_path, monkeypatch) as (entry, user, connect):
        socket, frame = await connect()
        original = entry.live.store.promote
        def refused(*args):
            raise StoreRefused("promotion is unavailable")
        monkeypatch.setattr(entry.live.store, "promote", refused)
        try:
            await document_control.handle_frame(socket, user, {
                "type": "doc_replace", "artifact_id": entry.artifact_id, "store_id": frame["store_id"],
                "text": "candidate", "request_id": "replace"})
            saved = await socket.next("doc_saved")
            assert saved["server_saved"] is False
            assert saved["pending"] is True and saved["held"] is None
        finally:
            monkeypatch.setattr(entry.live.store, "promote", original)


async def next_settled(socket):
    while True:
        frame = await socket.next("doc_pending")
        if not frame["pending"]:
            return frame


@pytest.mark.asyncio
async def test_retry_broadcasts_pending_clear_to_every_subscriber(ownership_scope, tmp_path, monkeypatch):
    async with transport(ownership_scope, tmp_path, monkeypatch) as (entry, user, connect):
        first, _ = await connect()
        second, _ = await connect()
        promote = await refuse_promotion(entry, monkeypatch, ownership_scope)
        monkeypatch.setattr(entry.live.store, "promote", promote)
        await entry.lane.retry.retry_blocked()
        for socket in (first, second):
            frame = await next_settled(socket)
            assert frame["artifact_id"] == entry.artifact_id
            assert frame["store_id"] == entry.live.store.store_id
            assert frame["held"] is None


@pytest.mark.asyncio
async def test_recovery_hold_broadcasts_held_instead_of_pending(ownership_scope, tmp_path, monkeypatch):
    async with transport(ownership_scope, tmp_path, monkeypatch) as (entry, user, connect):
        socket, _ = await connect()
        promote = await refuse_promotion(entry, monkeypatch, ownership_scope)
        entry.live.path.write_text("outside")
        monkeypatch.setattr(entry.live.store, "promote", promote)
        await entry.lane.retry.retry_blocked()
        frame = await next_settled(socket)
        assert frame["held"]["file_text"] == "outside"
        assert frame["held"]["candidate_text"] == "candidate"
        assert frame["held"]["choice_pending"] is False
