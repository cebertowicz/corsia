"""Owed store transitions stop retrying and expose durable, specific evidence."""
import asyncio

import pytest

from doceditor.live_lane import LaneRefused, Operation
from doceditor.live_store import StoreRefused
from doceditor.live_failure_record import FailureRecord
from tests.test_document_live_lane import open_lane
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_recovery_failure_blocks_with_reason_across_reload_and_explicit_retry(tmp_path, monkeypatch):
    live, lane = await open_lane(tmp_path)
    reason = "promotion refused: volume is read-only"
    failures = []
    calls = []
    promote = live.store.promote
    def refuse(*args):
        calls.append(args)
        raise StoreRefused(reason)
    monkeypatch.setattr(live.store, "promote", refuse)
    live.failure_changed = lambda kind, message, provenance: failures.append((kind, message))
    try:
        result = await lane.submit(Operation("replace", content="OWED"))
        assert not result.server_saved
        for _ in range(3):
            lane.retry._enqueue()
            await asyncio.shield(lane._worker)
            await asyncio.sleep(0)
        assert live.store.failure("store_refused") == FailureRecord(3, reason, 3)
        assert failures[-1] == ("store_refused", reason)
        assert lane.retry.timer is None
        assert len(calls) == 4  # original promotion plus exactly three retries
        with pytest.raises(LaneRefused, match="promotion refused: volume is read-only"):
            refused = await lane.submit(Operation("replace", content="CANNOT OVERTAKE"))
            assert refused.outcome == "refused"
            refused.require_landed()
        assert live.store.pending() is not None
        assert live.path.read_text() == "OWED"
        await lane.close()
        live.close()
        live, lane = await open_lane(tmp_path)
        promoted = live.store.promote
        def no_automatic(*args):
            raise AssertionError("exhausted recovery budget ran on load")
        monkeypatch.setattr(live.store, "promote", no_automatic)
        await lane.load()
        with pytest.raises(LaneRefused, match="promotion refused: volume is read-only"):
            refused = await lane.submit(Operation("flush"))
            assert refused.outcome == "refused"
            refused.require_landed()
        monkeypatch.setattr(live.store, "promote", promoted)
        await lane.retry.retry_blocked()
        assert live.store.pending() is None
        assert live.store.failure("store_refused") is None
        assert (await lane.submit(Operation("flush"))).server_saved
        assert live.text == live.path.read_text() == "OWED"
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_recovery_failure_uses_existing_transport_row_and_retry(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened
    from tests.test_document_live_transport import Socket
    from ws import document_control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    original = entry.live.store.promote
    def refused(*args):
        raise StoreRefused("specific promotion refusal")
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        actor = document_control._subscriptions[socket][entry.artifact_id].actor
        monkeypatch.setattr(entry.live.store, "promote", refused)
        assert not (await entry.lane.submit(Operation("replace", content="OWED", actor=actor))).server_saved
        for _ in range(3):
            entry.lane.retry._enqueue()
            await entry.lane._worker
            await asyncio.sleep(0)
        notice = await socket.next("doc_error")
        assert notice["kind"] == "store_refused" and notice["blocking"]
        assert notice["message"] == "specific promotion refusal"
        assert await document_control._blocking(entry) == [{"kind": "store_refused", "message": notice["message"], "failure_generation": 3}]
        frame = {"type": "doc_flush", "artifact_id": entry.artifact_id, "store_id": opened_frame["store_id"]}
        await document_control.handle_frame(socket, user, frame)
        assert (await socket.next("doc_error"))["message"] == notice["message"]
        monkeypatch.setattr(entry.live.store, "promote", original)
        await document_control.handle_frame(socket, user, {**frame, "retry": True})
        saved = await socket.next("doc_saved")
        assert saved["server_saved"] and saved["blocking"] == []
        assert entry.live.store.failure("store_refused") is None
    finally:
        monkeypatch.setattr(entry.live.store, "promote", original)
        await document_control.disconnect(socket)
        await manager.close()
