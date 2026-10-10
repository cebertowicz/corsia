"""Poisoned publication and identity setup stop retrying and remain actionable."""
import pytest
from doceditor.live_publisher import DocumentPublisher
from doceditor.live_failure_record import FailureRecord
from tests.test_document_live_publisher import setup, owe, owed
from tests.test_document_live_shared_lane import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_poisoned_first_publication_records_block_and_explicit_retry(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "first")
    owe(scope, store, "first", "second")
    original = owed(store)
    calls, notices = [], []
    def refused(store, row, **kwargs):
        calls.append(row.seq)
        raise PermissionError("registration permanently refused")
    monkeypatch.setattr(live_publisher, "publish_one", refused)
    publisher = DocumentPublisher(store)
    publisher.blocked = lambda kind, message, provenance: notices.append((kind, message))
    try:
        for _ in range(3):
            if publisher.retry:
                publisher.retry.cancel()
                publisher.retry = None
            await publisher._drain()
        assert publisher.retry is None
        assert owed(store) == original
        assert store.failure("publication_failed") == FailureRecord(3, "registration permanently refused", 3)
        assert notices == [("publication_failed", "registration permanently refused")]
        restarted = DocumentPublisher(type(store)(store.path))
        await restarted._drain()
        await restarted.close()
        assert calls == [original[0]] * 3
        published = []
        def success(store, row, **kwargs):
            published.append(row.seq)
            store.delete_publication(row.seq)
        monkeypatch.setattr(live_publisher, "publish_one", success)
        await publisher.retry_blocked()
        await publisher.task
        assert published == original
        assert store.failure("publication_failed") is None
    finally:
        await publisher.close()


@pytest.mark.asyncio
async def test_setup_failure_is_sticky_specific_and_not_rescheduled(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_lane import Operation
    from registry.shared_write_errors import SharedIdentitySetupRequired
    from registry import shared_documents
    scope = ownership_scope
    _write(scope, "live.md", "old")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    calls = []
    # A write discovers setup is needed, and its independent setup item fails.
    def missing(*args):
        raise SharedIdentitySetupRequired()
    monkeypatch.setattr(policy, "decision", missing)
    original = shared_documents.resolve_shared_target
    def denied(*args, **kwargs):
        calls.append(kwargs["thread_id"])
        raise PermissionError("identity registration denied")
    monkeypatch.setattr(shared_documents, "resolve_shared_target", denied)
    try:
        with pytest.raises(SharedIdentitySetupRequired):
            refused = await lane.submit(Operation("replace", content="first", actor=actor))
            assert refused.outcome == "refused"
            refused.require_landed()
        await lane._worker
        with pytest.raises(ValueError, match="identity registration denied"):
            refused = await lane.submit(Operation("replace", content="second", actor=actor))
            assert refused.outcome == "refused"
            refused.require_landed()
        assert len(calls) == 1
        assert policy.owner.setup_state(live.path)[0].reason == "identity registration denied"
        assert live.store.failure("setup_failed") is None
        assert type(live.store)(live.store.path).failure("setup_failed") is None
        assert live.path.read_text() == "old"
        monkeypatch.setattr(shared_documents, "resolve_shared_target", original)
        lane.schedule_setup(lambda: policy.setup(actor))
        await lane._worker
        assert live.path not in policy.owner.setup_failures
        assert live.store.failure("setup_failed") is None
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_transport_exposes_setup_reason_and_explicit_retry(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened as open_manager
    from tests.test_document_live_transport import Socket
    from ws import document_control
    scope = ownership_scope
    manager, entry, context = await open_manager(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    from registry import shared_documents
    original = shared_documents.resolve_shared_target
    calls = []
    def denied(*args, **kwargs):
        calls.append(kwargs["thread_id"])
        raise PermissionError("specific setup refusal")
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        actor = document_control._subscriptions[socket][entry.artifact_id].actor
        monkeypatch.setattr(shared_documents, "resolve_shared_target", denied)
        entry.lane.schedule_setup(lambda: entry.policy.setup(actor))
        await entry.lane._worker
        notice = await socket.next("doc_error")
        assert notice["kind"] == "setup_failed" and notice["blocking"]
        assert notice["message"] == "specific setup refusal"
        frame = {"type": "doc_flush", "artifact_id": entry.artifact_id, "store_id": opened_frame["store_id"]}
        await document_control.handle_frame(socket, user, frame)
        refusal = await socket.next("doc_error")
        assert refusal["kind"] == "setup_failed" and refusal["message"] == "specific setup refusal"
        assert len(calls) == 1
        monkeypatch.setattr(shared_documents, "resolve_shared_target", original)
        await document_control.handle_frame(socket, user, {**frame, "retry": True})
        saved = await socket.next("doc_saved")
        assert saved["server_saved"] and entry.live.store.failure("setup_failed") is None
        assert entry.live.path not in manager.setup_failures
        await entry.publisher.task
        for _ in range(3):
            entry.live.store.record_failure("publication_failed", "publication blocked")
        await document_control.handle_frame(socket, user, frame)
        refusal = await socket.next("doc_error")
        assert refusal["kind"] == "publication_failed" and refusal["message"] == "publication blocked"
        await document_control.handle_frame(socket, user, {**frame, "retry": True})
        saved = await socket.next("doc_saved")
        assert saved["blocking"] == []
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_blocked_flush_refusal_carries_the_blocking_broadcast_message(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    from tests.test_document_live_file_tools import opened as open_manager
    from tests.test_document_live_transport import Socket
    from ws import document_control
    scope = ownership_scope
    manager, entry, context = await open_manager(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    def refused(store, row, **kwargs):
        raise PermissionError("registration permanently refused")
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        frame = {"type": "doc_flush", "artifact_id": entry.artifact_id, "store_id": opened_frame["store_id"],
                 "request_id": "flush"}
        monkeypatch.setattr(live_publisher, "publish_one", refused)
        entry.publisher.delay = 0.01
        await document_control.handle_frame(socket, user, {**frame, "request_id": "owes"})
        assert (await socket.next("doc_saved"))["server_saved"]
        broadcast = await socket.next("doc_error")
        assert broadcast["blocking"] and "request_id" not in broadcast
        await document_control.handle_frame(socket, user, frame)
        refusal = await socket.next("doc_error")
        assert refusal["request_id"] == "flush" and refusal["kind"] == broadcast["kind"] == "publication_failed"
        assert refusal["message"] == broadcast["message"]
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_transport_announces_a_store_failure_cleared_by_a_landed_write(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened as open_manager
    from tests.test_document_live_transport import Socket
    from ws import document_control
    scope = ownership_scope
    manager, entry, context = await open_manager(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        entry.live.store.record_failure("store_refused", "disk full")
        await document_control.handle_frame(socket, user, {
            "type": "doc_flush", "artifact_id": entry.artifact_id, "store_id": opened_frame["store_id"]})
        cleared = await socket.next("doc_cleared")
        assert cleared["kind"] == "store_refused" and cleared["artifact_id"] == entry.artifact_id
        assert (await socket.next("doc_saved"))["server_saved"]
    finally:
        await document_control.disconnect(socket)
        await manager.close()
