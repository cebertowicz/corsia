"""Real policy and suggestion owners must not poison detached document writes."""
import asyncio

import pytest

from doceditor import comment_actions
from doceditor.document_scope import UI_THREAD
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from registry.shared_write_errors import DurableWritePolicyError
from tests.test_document_live_shared_lane import opened as shared_opened
from tests.test_document_live_file_tools import opened
from tests.test_document_live_lane import capture_jobs, settled_refusal
from tests.test_document_live_suggestions import suggestion, typing, legacy_writer, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write, _enforce


@pytest.mark.asyncio
async def test_detached_real_shared_policy_refusal_does_not_block_store(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    _write(scope, "live.md", "old")
    _enforce(scope)
    live, lane, policy, actor = await shared_opened(scope, tmp_path, "other")
    entered, release = asyncio.Event(), asyncio.Event()
    failures = []
    real = policy.decide
    async def held(operation, text):
        entered.set()
        await release.wait()
        try:
            return await real(operation, text)
        except DurableWritePolicyError as error:
            failures.append(error)
            raise
    lane.policy = held
    jobs = capture_jobs(lane, monkeypatch)
    events = []
    live.failure_changed = lambda kind, message, provenance: events.append((kind, message))
    call = asyncio.create_task(lane.submit(Operation("replace", content="refused", actor=actor)))
    try:
        await entered.wait()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        release.set()
        await lane._worker
        assert len(failures) == 1
        assert live.path.read_text() == live.text == "old"
        assert settled_refusal(jobs[0]) is failures[0]
        assert not lane.retry.blocked_reason
        assert live.store.failure("store_refused") is None
        assert not events
        lane.policy = real
        owner = WriteActor(scope["owner"], "project-alpha")
        assert (await lane.submit(Operation("replace", content="owner succeeds", actor=owner))).server_saved
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_detached_real_accept_stale_refusal_does_not_block_store(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    typing(entry, 2, delete=1, insert="changed")
    entered, release = asyncio.Event(), asyncio.Event()
    real_import = entry.lane._import
    async def held():
        result = await real_import()
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(entry.lane, "_import", held)
    jobs = capture_jobs(entry.lane, monkeypatch)
    events = []
    entry.live.failure_changed = lambda kind, message, provenance: events.append((kind, message))
    call = asyncio.create_task(comment_actions.decide_suggestion(row["id"], "accept", user_id=user_id(scope),
        decided_by="user", actor_thread_id=None, write=legacy_writer))
    try:
        await entered.wait()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        release.set()
        await entry.lane._worker
        assert str(settled_refusal(jobs[0])) == comment_actions.STALE_MESSAGE
        assert entry.live.path.read_text() == "a b\nsecond\n"
        assert not entry.lane.retry.blocked_reason
        assert entry.live.store.failure("store_refused") is None
        assert not events
        monkeypatch.setattr(entry.lane, "_import", real_import)
        user = WriteActor(UI_THREAD, "project-alpha", direct_user=True, user_id=user_id(scope))
        assert (await entry.lane.submit(Operation("flush", actor=user))).server_saved
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await manager.close()
