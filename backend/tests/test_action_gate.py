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


CALL = "call-1"


def _deliver(db, turn_id):
    """Give the turn's claimed inputs their transcript position, as the provider write does."""
    from infra.input_writes import sequence_attempt

    rows = db.execute(
        "SELECT id, thread_id FROM messages WHERE active_turn_id=? AND delivery_state='queued'"
        " ORDER BY input_order",
        (turn_id,),
    ).fetchall()
    if rows:
        sequence_attempt(db, rows, state="delivered", outcome="accepted", error=None)
        db.commit()


def _declare(db, turn_id, call_id=CALL, thread_id=THREAD, response=None):
    """Persist the Assistant Step that declares one tool call."""
    from infra.queries import insert_message

    metadata = {
        "turn_id": turn_id,
        "llm_tool_calls": [{"id": call_id, "function": {"name": "email_send", "arguments": "{}"}}],
    }
    if response:
        metadata["provider_message_id"] = response
    insert_message(thread_id, "assistant", "", metadata, db=db)
    db.commit()


def _bind(db, turn_id, thread_id=THREAD, *, deliver=True, declare=True, call_id=CALL):
    """Run as a live tool call does: delivered input, then the step that declared the call."""
    from core.turn_core import TurnIdentity
    from infra import state

    if deliver:
        _deliver(db, turn_id)
    if declare:
        _declare(db, turn_id, call_id, thread_id)
    order = db.execute("SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)).fetchone()[0]
    state._current_thread_id.set(thread_id)
    state._current_turn_identity.set(TurnIdentity(turn_id, int(order)))
    state._current_provider_call_id.set(call_id)
    state._current_tool_invocation_id.set(None)


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
    from tools.action_drafts import claim_for_send, mark_sent

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    claim_for_send(draft["draft_id"], db=deleg_db)
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


@pytest.mark.asyncio
async def test_message_delivered_after_the_declaring_step_cannot_approve_the_call(
    deleg_db, workspace,
):
    """A steer claimed and delivered after the call was emitted was never seen by the call."""
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1", deliver=False)  # the declaring step is written first
    _deliver(deleg_db, "t1")              # the person's row reaches the model afterwards
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_steer_between_rows_of_one_parallel_batch_cannot_approve_its_later_call(
    deleg_db, workspace,
):
    """Claude Code writes one row per call of a batch; a steer landing between them was unseen."""
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _declare(deleg_db, "t1", "call-0", response="msg-1")   # call A's row
    _deliver(deleg_db, "t1")                                 # the steer lands while A is held
    _declare(deleg_db, "t1", CALL, response="msg-1")         # call B, same model response
    _bind(deleg_db, "t1", deliver=False, declare=False)
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_person_message_before_a_later_response_approves_its_call(deleg_db, workspace):
    """A response written after the person's row saw it, whatever earlier responses did."""
    from tools.action_gate import Approved

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _declare(deleg_db, "t1", "call-0", response="msg-1")
    _deliver(deleg_db, "t1")
    _declare(deleg_db, "t1", CALL, response="msg-2")
    _bind(deleg_db, "t1", deliver=False, declare=False)
    assert isinstance(await _gate(draft), Approved)


@pytest.mark.asyncio
async def test_message_claimed_but_not_delivered_is_refused(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1", deliver=False)
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_call_with_no_declaring_step_is_refused(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1", declare=False)
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_call_with_no_call_id_is_refused(deleg_db, workspace):
    from infra import state
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    state._current_provider_call_id.set(None)
    assert _text(await _gate(draft)) == action_gate.NOT_PERSON


@pytest.mark.asyncio
async def test_invocation_id_is_used_when_no_provider_call_id_is_bound(deleg_db, workspace):
    from infra import state
    from tools.action_gate import Approved

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    state._current_provider_call_id.set(None)
    state._current_tool_invocation_id.set(CALL)
    assert isinstance(await _gate(draft), Approved)


@pytest.mark.asyncio
async def test_timestamps_compare_as_instants_not_strings(deleg_db, workspace):
    from tools import action_gate
    from tools.action_gate import Approved

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    deleg_db.execute("UPDATE action_drafts SET recorded_at='2026-10-14T10:00:00' WHERE id=?",
                     (draft["draft_id"],))
    for stamp, passes in (
        ("2026-10-14T10:00:00.500000+00:00", True),   # later, rendered with an offset
        ("2026-10-14T03:00:00.500000-07:00", True),   # the same instant in another offset
        ("2026-10-14T09:59:59", False),               # earlier
    ):
        deleg_db.execute("UPDATE messages SET created_at=? WHERE id='t1-input'", (stamp,))
        deleg_db.commit()
        result = await _gate(draft)
        if passes:
            assert isinstance(result, Approved), stamp
        else:
            assert _text(result) == action_gate.NOT_SEEN


@pytest.mark.asyncio
async def test_unparseable_timestamp_is_refused(deleg_db, workspace):
    from tools import action_gate

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    deleg_db.execute("UPDATE action_drafts SET recorded_at='not a time' WHERE id=?",
                     (draft["draft_id"],))
    deleg_db.commit()
    assert _text(await _gate(draft)) == action_gate.NOT_SEEN


@pytest.mark.asyncio
async def test_path_that_leaves_the_workspace_is_changed_outside(deleg_db, workspace, tmp_path):
    from tools import action_gate
    from tools.action_drafts import create_draft

    (tmp_path / "outside.md").write_bytes(BODY)
    made = create_draft(THREAD, "email", "../outside.md", BODY, db=deleg_db)
    deleg_db.commit()
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    assert _text(await _gate(made)) == action_gate.CHANGED_OUTSIDE


@pytest.mark.asyncio
async def test_symlink_to_outside_the_workspace_is_changed_outside(deleg_db, workspace, tmp_path):
    from tools import action_gate
    from tools.action_drafts import create_draft

    outside = tmp_path / "secret.md"
    outside.write_bytes(BODY)
    (workspace / "link.md").symlink_to(outside)
    made = create_draft(THREAD, "email", "link.md", BODY, db=deleg_db)
    deleg_db.commit()
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    assert _text(await _gate(made)) == action_gate.CHANGED_OUTSIDE


def test_send_claim_is_taken_once_then_released_or_completed(deleg_db, workspace):
    from tools.action_drafts import DraftError, claim_for_send, mark_sent, release_claim

    draft_id = _draft(deleg_db, workspace)["draft_id"]
    claim_for_send(draft_id, db=deleg_db)
    with pytest.raises(DraftError):
        claim_for_send(draft_id, db=deleg_db)         # a parallel or retried send
    release_claim(draft_id, db=deleg_db)
    with pytest.raises(DraftError):
        release_claim(draft_id, db=deleg_db)          # no longer claimed
    with pytest.raises(DraftError):
        mark_sent(draft_id, "gmail-1", db=deleg_db)   # must be claimed first
    claim_for_send(draft_id, db=deleg_db)
    mark_sent(draft_id, "gmail-1", db=deleg_db)
    row = deleg_db.execute(
        "SELECT state, external_id, sent_at FROM action_drafts WHERE id=?", (draft_id,),
    ).fetchone()
    assert (row[0], row[1]) == ("sent", "gmail-1") and row[2]
    for transition in (claim_for_send, release_claim):
        with pytest.raises(DraftError):
            transition(draft_id, db=deleg_db)


@pytest.mark.asyncio
async def test_draft_being_sent_is_refused_by_the_gate(deleg_db, workspace):
    from tools import action_gate
    from tools.action_drafts import claim_for_send

    draft = _draft(deleg_db, workspace)
    _admit(deleg_db, "t1")
    _bind(deleg_db, "t1")
    claim_for_send(draft["draft_id"], db=deleg_db)
    deleg_db.commit()
    assert _text(await _gate(draft)) == action_gate.ALREADY_DONE
