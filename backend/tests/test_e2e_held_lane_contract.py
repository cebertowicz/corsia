"""Integration contracts for fault controls on the real opened shared lane."""
import asyncio

import pytest

from e2e_live_faults import FaultConfiguration, FaultController
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from tests.test_e2e_live_faults import fault_environment
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_prepare_held_uses_current_viewer_actor_and_real_lane(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    entry.live.viewer_actor = lambda: actor
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    try:
        with controller.install_checkpoint_hook():
            result = await controller.prepare_held(entry.artifact_id, "# Outside file\n")
        assert result["pending"] == "held"
        assert entry.live.path.read_text() == "# Outside file\n"
        assert entry.live.store.pending().choice is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_binding_preserves_operation_admission(ownership_scope, tmp_path, monkeypatch):
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
        assert calls, "Fault fixture bypassed canonical publication admission"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_bound_started_operation_settles_after_real_caller_cancellation(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_lane
    from e2e_live_faults import _active_artifact
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    controller._bind(entry.artifact_id)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def checkpoint(stage):
        if stage == "record":
            observed.append(_active_artifact.get())
            entered.set()
            await release.wait()

    monkeypatch.setattr(live_lane, "_checkpoint", checkpoint)
    operation = Operation("replace", content="STARTED RESULT", actor=WriteActor(ownership_scope["owner"], "project-alpha"))
    task = asyncio.create_task(entry.lane.submit(operation))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel("caller stopped")
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await entry.lane._worker
        assert observed == [entry.artifact_id]
        assert entry.live.text == entry.live.path.read_text() == "STARTED RESULT"
        assert entry.live.store.pending() is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
