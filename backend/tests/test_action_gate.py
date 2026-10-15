"""The send gate: the person wrote in this live turn, after the draft's last recorded save."""
from __future__ import annotations

import os

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.time import utc_now_iso

THREAD = "sender-self"
BODY = b"To: sam@example.com\nSubject: Lunch\n\nNoon?\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from infra import config

    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", root)
    return root


def _admit(db, turn_id, *, metadata=None, thread_id=THREAD):
    from infra.thread_driver import admit_input

    admission = admit_input(
        thread_id, role="user", content="send it", intent="apply_now",
        message_id=f"{turn_id}-input", turn_id=turn_id, boot_epoch="test",
        admitted_at=utc_now_iso(), metadata=metadata, db=db,
    )
    return admission


def _bind(db, turn_id, thread_id=THREAD):
    """Bind the turn identity the way a running tool call sees it."""
    from core.turn_core import TurnIdentity
    from infra import state

    order = db.execute("SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)).fetchone()[0]
    state._current_thread_id.set(thread_id)
    state._current_turn_identity.set(TurnIdentity(turn_id, int(order)))


def _draft(db, workspace, data=BODY, kind="email", thread_id=THREAD):
    from tools.action_drafts import create_draft

    (workspace / "draft.md").write_bytes(data)
    made = create_draft(thread_id, kind, "draft.md", data, {"reply_to": "m1"}, db=db)
    db.commit()
    return made


async def _gate(draft, kind="email", revision=None):
    from tools.action_gate import check_action

    return await check_action(kind, draft["draft_id"], revision or draft["revision"])


def _text(result):
    from tools.action_gate import Refusal

    assert isinstance(result, Refusal), result
    return result.text


@pytest.mark.asyncio
async def test_person_wrote_after_the_recorded_draft_passes(deleg_db, workspace):
    from tools.action_gate import Approved

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    result = await _gate(draft)
    assert isinstance(result, Approved)
    assert result.data == BODY and result.draft["id"] == draft["draft_id"]


@pytest.mark.asyncio
async def test_no_turn_identity_is_refused(deleg_db, workspace):
    from infra import state
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    state._current_thread_id.set(THREAD)
    state._current_turn_identity.set(None)
    assert _text(await _gate(draft)) == action_gate.NO_TURN


@pytest.mark.asyncio
async def test_turn_that_is_no_longer_running_is_refused(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    deleg_db.execute("UPDATE active_turns SET status='completed' WHERE id='t1'")
    deleg_db.commit()
    assert _text(await _gate(draft)) == action_gate.NO_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    [
        {"routed_event": "email.received"},          # event wake
        {"sender_kind": "agent", "from_thread": "x"},  # agent send
        {"completion": {"child": "x"}},              # parent completion
    ],
)
async def test_agent_started_turn_is_refused(deleg_db, workspace, marker):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1", metadata=marker)
    _bind(deleg_db, "t1")
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_person_message_queued_behind_an_agent_turn_does_not_count(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1", metadata={"routed_event": "email.received"})
    queued = _admit(deleg_db, "t2")
    assert queued.batch is None  # waits behind the running turn
    _bind(deleg_db, "t1")
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_unknown_foreign_and_wrong_kind_drafts_have_no_draft(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    assert _text(await _gate({"draft_id": "nope", "revision": "x"})) == action_gate.NO_DRAFT
    assert _text(await _gate(draft, kind="invite")) == action_gate.NO_DRAFT
    deleg_db.execute("UPDATE action_drafts SET thread_id='other-thread' WHERE id=?",
                     (draft["draft_id"],))
    # No such thread row: the foreign key is off in this fixture, the gate still refuses.
    assert _text(await _gate(draft)) == action_gate.NO_DRAFT


@pytest.mark.asyncio
async def test_sent_draft_is_refused(deleg_db, workspace):
    from tools import action_gate
    from tools.action_drafts import mark_sent

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    mark_sent(draft["draft_id"], "gmail-1", db=deleg_db)
    deleg_db.commit()
    assert _text(await _gate(draft)) == action_gate.ALREADY_DONE


@pytest.mark.asyncio
async def test_file_rewritten_after_recording_is_changed_outside_even_with_old_mtime(
    deleg_db, workspace,
):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    path = workspace / "draft.md"
    before = path.stat()
    path.write_bytes(b"To: evil@example.com\n\nSend money\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _text(await _gate(draft)) == action_gate.CHANGED_OUTSIDE


@pytest.mark.asyncio
async def test_missing_document_is_changed_outside(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    (workspace / "draft.md").unlink()
    assert _text(await _gate(draft)) == action_gate.CHANGED_OUTSIDE


@pytest.mark.asyncio
async def test_revision_mismatch_is_stale(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    assert _text(await _gate(draft, revision="0" * 64)) == action_gate.STALE_REVISION


@pytest.mark.asyncio
async def test_draft_recorded_after_the_persons_message_is_not_seen(deleg_db, workspace):
    from tools import action_gate
    from tools.action_drafts import record_draft

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    new = b"To: sam@example.com\nSubject: Lunch\n\nNoon at the cafe?\n"
    (workspace / "draft.md").write_bytes(new)
    saved = record_draft(draft["draft_id"], new, db=deleg_db)
    deleg_db.commit()
    result = await _gate({"draft_id": draft["draft_id"], "revision": saved["revision"]})
    assert _text(result) == action_gate.NOT_SEEN


@pytest.mark.asyncio
async def test_open_document_is_read_from_the_live_text(deleg_db, workspace, monkeypatch):
    from doceditor import live_tools
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")

    class _Live:
        text = "To: sam@example.com\n\nEdited by the person in the editor\n"

    class _Entry:
        live = _Live()

    monkeypatch.setattr(live_tools, "find_live", lambda path: _Entry())
    assert _text(await _gate(draft)) == action_gate.CHANGED_OUTSIDE


def test_refusal_texts_match_the_design():
    from tools import action_gate

    assert action_gate.NOT_SEEN.startswith("Not sent: the person hasn't replied since")
    assert action_gate.CHANGED_OUTSIDE.startswith("Not sent: the draft was changed outside")
    assert action_gate.STALE_REVISION.startswith("Not sent: the draft has changed since")
