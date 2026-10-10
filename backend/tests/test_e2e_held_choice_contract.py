"""Real shared-lane control semantics, including refused choices and resend."""
import asyncio

import pytest

from e2e_live_faults import FaultConfiguration, FaultController
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import BrowserMessage, StoreRefused
from doceditor.live_text import clone
from tests.test_e2e_live_faults import fault_environment
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


async def held(scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    actor = WriteActor(scope["owner"], "project-alpha")
    entry.live.viewer_actor = lambda: actor
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path / "control")))
    with controller.install_checkpoint_hook():
        await controller.prepare_held(entry.artifact_id, "OUTSIDE\n")
    return manager, entry, actor, controller


def browser_message(entry, edit):
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    if edit == "insert":
        peer["text"].insert(0, "LATE ")
    else:
        del peer["text"][4:11]
    return BrowserMessage("received-uncommitted", peer.get_update(vector)), str(peer["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["insert", "delete"])
@pytest.mark.parametrize("choice,refusal", [("corsia", None), ("file", None),
                                          ("corsia", "record"), ("corsia", "rename"),
                                          ("corsia", "clear"), ("file", "apply")])
async def test_received_choice_refusal_retry_resend_and_reopen(ownership_scope, tmp_path, monkeypatch, edit, choice, refusal):
    manager, entry, actor, controller = await held(ownership_scope, tmp_path, monkeypatch)
    acked = []
    entry.live.acknowledge = acked.extend
    message, edited = browser_message(entry, edit)
    try:
        controller.refuse_once(entry.artifact_id, "commit")
        assert entry.live.receive(message)
        with pytest.raises(StoreRefused):
            await entry.live.commit_received()
        assert acked == []
        assert controller.status(entry.artifact_id)["received_ids"] == [message.message_id]
        assert str(entry.live.durable["text"]) == "a b\nsecond\n"
        if refusal:
            controller.refuse_once(entry.artifact_id, refusal)
            with pytest.raises((StoreRefused, PermissionError)):
                await entry.lane.choose(choice, actor=actor)
            assert entry.live.store.pending().state == "held"
            assert entry.live.text == edited
            assert entry.live.path.read_text() == "OUTSIDE\n"
            await entry.lane.recover()
            assert entry.live.store.pending().choice is None
        await entry.lane.choose(choice, actor=actor)
        expected = "OUTSIDE\n" if choice == "file" else edited
        evidence = await controller.evidence(entry.artifact_id)
        assert evidence["pending"] is None
        assert evidence["received_ids"] == []
        assert [evidence[key] for key in ("live_text", "durable_text", "mirror_text", "file_text")] == [expected] * 4
        assert evidence["live_state_digest"] == evidence["durable_state_digest"] == evidence["mirror_state_digest"]
        assert acked == [message.message_id]
        identity = entry.live.doc.get_update()
        # Genuine identical browser resend, not a newly synthesized text replacement.
        assert entry.live.receive(message)
        await entry.live.commit_received()
        assert entry.live.doc.get_update() == identity
        assert entry.live.text == expected
        assert entry.live.path.read_text() == expected
        assert acked == [message.message_id, message.message_id]
        # Reconnect uses a NEW envelope ID for the current CRDT delta/deletion set.
        sync = BrowserMessage("reopen-sync", entry.live.doc.get_update(entry.live.durable.get_state()))
        assert entry.live.receive(sync)
        await entry.live.commit_received()
        assert entry.live.doc.get_update() == identity
        assert acked[-1] == sync.message_id
        artifact_id, path, store_id = entry.artifact_id, entry.live.path, entry.live.store.store_id
        await manager.evict(artifact_id, entry, can_evict=lambda: True)
        reopened = await manager.open(artifact_id, path)
        assert reopened is not entry
        assert reopened.live.store.store_id == store_id
        assert reopened.live.doc.get_update() == identity
        assert reopened.live.text == path.read_text() == expected
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,stage", [("corsia", "choice_apply"), ("file", "choice_apply"), ("corsia", "choice_clear")])
async def test_cut_is_inside_real_transaction_before_commit(ownership_scope, tmp_path, monkeypatch, choice, stage):
    manager, entry, actor, controller = await held(ownership_scope, tmp_path, monkeypatch)
    message, _ = browser_message(entry, "insert")
    entry.live.receive(message)
    reached = []

    class Interrupted(RuntimeError):
        pass

    def stop(cut):
        if cut == stage:
            reached.append(cut)
            raise Interrupted("test transaction interruption")

    monkeypatch.setattr(controller, "kill_at", stop)
    try:
        controller.begin_case()
        if stage == "choice_clear":
            controller.refuse_once(entry.artifact_id, "rename")
        controller.arm(entry.artifact_id, stage)
        with controller.install_checkpoint_hook(), pytest.raises(Interrupted):
            await entry.lane.choose(choice, actor=actor)
        assert reached == [stage]
        # A transaction cut has not removed the original held evidence.
        pending = entry.live.store.pending()
        assert pending.state == "held"
        assert (pending.choice is None) == (choice == "file")
        assert entry.live.path.read_text() == ("LATE a b\nsecond\n" if stage == "choice_apply" and choice == "corsia" else "OUTSIDE\n")
    finally:
        await manager.close()
