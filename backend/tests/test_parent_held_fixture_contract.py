"""Parent-only failure-first probes of the rejected held fixture."""
import pytest

from e2e_live_faults import FaultConfiguration, FaultController
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from tests.test_e2e_live_faults import fault_environment
from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_held_preparation_reaches_real_shared_lane(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    from ws import document_control
    scope = ownership_scope
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    socket = Socket()
    # Match the browser's actual writable doc_open, rather than fabricating its actor.
    await document_control.handle_frame(socket, {"id": user_id}, {
        "type": "doc_open", "path": str(entry.live.path), "project_id": "project-alpha",
        "thread_id": scope["owner"], "request_id": "parent-held-open",
    })
    frame = await socket.next("doc_opened")
    assert frame["live"] is True and frame["writable"] is True
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    try:
        with controller.install_checkpoint_hook():
            result = await controller.prepare_held(entry.artifact_id, "# Outside file\n")
        assert result["pending"] == "held"
        assert entry.live.viewer_actor().direct_user is True
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_held_preparation_refuses_without_a_writable_viewer(ownership_scope, tmp_path, monkeypatch):
    from fastapi import HTTPException
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    try:
        with pytest.raises(HTTPException) as refused:
            await controller.prepare_held(entry.artifact_id, "# Outside file\n")
        assert refused.value.status_code == 409
        assert entry.live.store.pending() is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_bound_fixture_keeps_canonical_publication_admission(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    calls = []
    original = entry.lane.admission.admit

    async def observed(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(entry.lane.admission, "admit", observed)
    controller._bind(entry.artifact_id)
    try:
        result = await entry.lane.submit(Operation(
            "replace", content=entry.live.text,
            actor=WriteActor(ownership_scope["owner"], "project-alpha"),
        ))
        assert result.outcome == "landed"
        assert calls, "Fixture bypassed canonical publication admission"
    finally:
        await manager.close()
