"""Document product events come from one settle point and carry no text or paths."""
import asyncio
import json

import pytest

from doceditor.live_lane import Operation
from tests.test_document_live_lane import cut_at, open_lane
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


def capture(monkeypatch):
    from doceditor import live_events
    seen = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: seen.append((action, properties)))
    return seen


def outcomes(seen):
    return [(p["operation"], p["outcome"], p["detached"]) for action, p in seen if action == "lane_outcome"]


def assert_private(seen, *secrets):
    for _, properties in seen:
        payload = json.dumps(properties)
        assert not any(secret in payload for secret in secrets), payload


@pytest.mark.asyncio
async def test_each_lane_outcome_is_emitted_once_without_text(tmp_path, monkeypatch):
    from doceditor import live_lane
    seen = capture(monkeypatch)
    monkeypatch.setattr(live_lane, "NATIVE_SETTLE_TIMEOUT_S", .02, raising=False)
    live, lane = await open_lane(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    call = None
    try:
        await lane.submit(Operation("replace", content="TEXT-ZERO"))
        await lane.submit(Operation("write", content="TEXT-UNSAFE", base_hash="unknown"))
        cut_at(monkeypatch, cut)
        first, queued = asyncio.Event(), asyncio.Event()
        call = asyncio.create_task(lane.submit(Operation("replace", content="TEXT-ONE"), cancel_event=first))
        await entered.wait()
        queued.set()
        await lane.submit(Operation("replace", content="TEXT-NEVER"), cancel_event=queued)
        first.set()
        assert (await call).outcome == "pending"
        release.set()
        await lane._worker
        assert outcomes(seen) == [
            ("replace", "landed", False), ("write", "refused", False), ("replace", "not_started", False),
            ("replace", "pending", True), ("replace", "landed", True),
        ]
        assert_private(seen, "TEXT-", str(tmp_path))
    finally:
        release.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_closed_lane_refusal_is_an_outcome(tmp_path, monkeypatch):
    seen = capture(monkeypatch)
    live, lane = await open_lane(tmp_path)
    await lane.close()
    try:
        assert (await lane.submit(Operation("replace", content="TEXT-LATE"))).outcome == "refused"
        assert outcomes(seen) == [("replace", "refused", False)]
    finally:
        live.close()


@pytest.mark.asyncio
async def test_held_choice_resolution_is_emitted_once(tmp_path, monkeypatch):
    from tests.test_document_live_recovery import held
    live, lane = await held(tmp_path, monkeypatch)
    seen = capture(monkeypatch)
    try:
        await lane.choose("corsia")
        choices = [p for action, p in seen if action == "choice_resolved"]
        assert [(p["choice"], p["server_saved"]) for p in choices] == [("corsia", True)]
        assert_private(seen, "OUTSIDE", "USER", str(tmp_path))
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_live_open_and_legacy_refusal_carry_the_artifact_id(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened
    from tests.test_document_live_legacy_save import old_client, save_old
    seen = capture(monkeypatch)
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    try:
        async with old_client(ownership_scope, monkeypatch) as client:
            assert (await save_old(client, entry.live.path, "TEXT-LEGACY")).status_code == 409
        named = [(action, p.get("artifact_id"), p.get("code")) for action, p in seen if action != "lane_outcome"]
        assert named == [("live_opened", entry.artifact_id, None),
                         ("legacy_save_refused", entry.artifact_id, "document_live")]
        assert_private(seen, "TEXT-", str(entry.live.path))
    finally:
        await manager.close()
