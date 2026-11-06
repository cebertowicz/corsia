"""A comment thread's writer turn: agent writes wait in line while someone else holds it, and are served in order."""

from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import _OWNER, _comment_by_thread_a, _mk_thread, _patch, _post, _registrable_roots, doc

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_turns():
    from doceditor import writer_turns
    from ws import comment_turn

    writer_turns.reset()
    yield
    for handle in comment_turn._leases.values():
        handle.cancel()
    comment_turn._leases.clear()
    comment_turn._order.clear()
    writer_turns.reset()


@pytest.fixture
def driven(monkeypatch):
    from plans import public

    calls: list[str] = []

    async def drive_threads(thread_ids):
        calls.extend(thread_ids)

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    return calls


def _rows(sql: str, *args) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _line(root_id: str) -> list[dict]:
    return _rows("SELECT * FROM doc_comment_writer_line WHERE root_id=? ORDER BY saved_at,id", root_id)


def _replies(root_id: str, thread_id: str) -> list[dict]:
    return _rows("SELECT * FROM doc_comments WHERE parent_id=? AND author_thread_id=?", root_id, thread_id)


def _wake(row: dict) -> dict:
    [message] = _rows("SELECT * FROM messages WHERE id=?", row["wake_message_id"])
    return message


def _artifact_id(root_id: str) -> str:
    return _rows("SELECT artifact_id FROM doc_comments WHERE id=?", root_id)[0]["artifact_id"]


async def _frame(page, root_id: str, on: bool) -> None:
    from infra.auth import authenticate
    from ws import comment_turn

    await comment_turn.handle_frame(
        page, authenticate(_OWNER), {"type": "comment_turn", "comment_id": root_id, "on": on})


def _wake_turn(thread_id: str, row: dict, *, status: str = "running", read: bool = True) -> str:
    """The woken agent's turn, holding the wake: read (delivered into it) or claimed and not yet written."""
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db, write_transaction

    turn_id = f"turn-{row['id']}-{row['wakes']}"
    db = get_db()
    try:
        with write_transaction(db, scope="test_wake_turn") as connection:
            connection.execute(
                """INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
                   VALUES (?,?,0,'native/test',?,'t0',?)""", (turn_id, thread_id, status, BOOT_EPOCH))
            connection.execute(
                "UPDATE messages SET delivery_state=?, active_turn_id=? WHERE id=?",
                ("delivered" if read else "queued", turn_id, row["wake_message_id"]))
    finally:
        db.close()
    return turn_id


def _end(thread_id: str, turn_id: str, ending: str):
    """End the turn the way the harness or the person does; every way runs the one terminal write."""
    from core.turn_core import persist_turn_settlement
    from ws.stop_control import _commit_stop_terminal

    if ending == "completed":
        return persist_turn_settlement(turn_id, "completed")
    if ending == "error":
        return persist_turn_settlement(turn_id, "error", "provider failed")
    [owner] = _rows("SELECT user_id FROM threads WHERE id=?", thread_id)
    status, applied = _commit_stop_terminal(owner["user_id"], thread_id, turn_id)
    assert status == "cancelled" and applied is not None
    return applied[1]


@pytest.mark.asyncio
async def test_a_reply_waits_while_a_person_is_on_the_thread_and_posts_as_is_exactly_once(
    integration_client, doc, _registrable_roots, driven,
):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    page = object()
    await _frame(page, root, True)

    saved = await DocCommentCreateTool().execute(body="B's reply", parent_id=root, _thread_id=b)

    assert saved.startswith("Saved, 1st in line behind ")
    assert "You'll be woken with what changed. Don't act on this anywhere else until it posts." in saved
    assert _replies(root, b) == []
    [row] = _line(root)
    assert row["state"] == "waiting" and driven == []

    await _frame(page, root, False)

    [row] = _line(root)
    assert row["state"] == "woken" and driven == [b]
    wake = _wake(row)
    assert wake["thread_id"] == b
    assert 'Reply: "B\'s reply"' in wake["content"]
    assert f'doc_comment_update(comment_id="{root}", post_as_is=true)' in wake["content"]

    posted = json.loads(await DocCommentUpdateTool().execute(comment_id=root, post_as_is=True, _thread_id=b))
    again = await DocCommentUpdateTool().execute(comment_id=root, post_as_is=True, _thread_id=b)

    assert [item["comment"]["body"] for item in posted["posted"]] == ["B's reply"]
    assert again == "Error: You have nothing saved in line on this thread."
    assert [reply["body"] for reply in _replies(root, b)] == ["B's reply"]
    assert _line(root) == []  # a posted row is deleted


@pytest.mark.asyncio
async def test_two_agents_in_line_are_served_in_order_and_the_second_wakes_when_the_first_write_lands(
    integration_client, doc, _registrable_roots, driven,
):
    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    c = _mk_thread(integration_client, _OWNER, title="Agent C")
    page = object()
    await _frame(page, root, True)

    first = _post(integration_client, _OWNER, parent_id=root, body="B first", as_session_id=b)
    second = _post(integration_client, _OWNER, parent_id=root, body="C second", as_session_id=c)

    assert first.json()["queued"]["position"] == 1
    assert second.json()["queued"]["position"] == 2
    assert second.json()["queued"]["message"].startswith("Saved, 2nd in line behind ")

    await _frame(page, root, False)
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken"), (c, "waiting")]
    assert driven == [b]

    rewrite = _post(integration_client, _OWNER, parent_id=root, body="B, rewritten", as_session_id=b)

    assert rewrite.status_code == 200, rewrite.text
    assert rewrite.json()["comment"]["body"] == "B, rewritten"
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]
    assert driven == [b, c]
    assert [reply["body"] for reply in _replies(root, b)] == ["B, rewritten"]
    assert _replies(root, c) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending, read", [
    ("completed", True), ("error", True), ("stopped", True), ("stopped", False),
], ids=["ended", "failed", "stopped", "stopped-before-reading"])
async def test_an_agent_whose_turn_ends_without_answering_drops_out_is_told_and_the_next_is_woken(
    integration_client, doc, _registrable_roots, driven, ending, read,
):
    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    c = _mk_thread(integration_client, _OWNER, title="Agent C")
    page = object()
    await _frame(page, root, True)
    _post(integration_client, _OWNER, parent_id=root, body="B never posts this", as_session_id=b)
    _post(integration_client, _OWNER, parent_id=root, body="C waits", as_session_id=c)
    await _frame(page, root, False)
    woken_b = _line(root)[0]

    settlement = _end(b, _wake_turn(b, woken_b, read=read), ending)

    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]
    assert c in settlement.wake_thread_ids  # driven once the harness releases the turn
    notes = _rows(
        "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", b)
    assert any("dropped out of line and was not posted" in n["summary"] and "B never posts this" in n["summary"]
               for n in notes)
    assert _replies(root, b) == []


@pytest.mark.asyncio
async def test_a_three_message_burst_from_the_person_reaches_the_agent_as_one_wake(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    """One wake, and one change frame sent after delivery: never while the burst's batch is open."""
    from doceditor import comment_notifications, feedback_batches

    a, root = _comment_by_thread_a(integration_client, doc)
    artifact_id = _artifact_id(root)
    open_at_frame: list[int] = []
    monkeypatch.setattr(
        comment_notifications, "broadcast_doc_comment_changed",
        lambda frame_artifact_id, **_kwargs: frame_artifact_id == artifact_id and open_at_frame.append(len(_rows(
            "SELECT id FROM doc_feedback_batches WHERE artifact_id=? AND state='open'", frame_artifact_id))),
    )
    for body in ("First thought.", "Second thought.", "typo: thought"):
        response = integration_client.post(
            f"/api/artifacts/{artifact_id}/comments?token={_OWNER}",
            json={"body": body, "parent_id": root, "delivery_policy": "immediate"})
        assert response.status_code == 200, response.text

    [batch] = _rows("SELECT * FROM doc_feedback_batches WHERE recipient_id=? AND author_kind='user'", a)
    assert (batch["state"], batch["item_count"]) == ("open", 3)
    assert driven == [] and open_at_frame == []

    woken = feedback_batches.close_due(batch["closes_at"])

    # close_due closes every due batch in the shared database, including ones earlier tests left open,
    # so the wake and frame checks count this thread and this document only.
    assert woken.count(a) == 1
    [batch] = _rows("SELECT * FROM doc_feedback_batches WHERE recipient_id=? AND author_kind='user'", a)
    assert (batch["state"], batch["item_count"]) == ("delivered", 3)
    assert open_at_frame == [0]


@pytest.mark.asyncio
async def test_a_saved_write_survives_a_restart_and_is_woken_after_it(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import writer_turns
    from doceditor.agent_comment_writes import recover_line
    from infra.db_async import run_in_db

    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    c = _mk_thread(integration_client, _OWNER, title="Agent C")
    page = object()
    await _frame(page, root, True)
    _post(integration_client, _OWNER, parent_id=root, body="B survives", as_session_id=b)
    _post(integration_client, _OWNER, parent_id=root, body="C survives", as_session_id=c)
    await _frame(page, root, False)
    woken_b = _line(root)[0]
    _wake_turn(b, woken_b, status="completed")  # the crash took B's turn with it; no turn end was recorded
    writer_turns.reset()  # a restart forgets every turn

    woken = await run_in_db(recover_line)  # serves every line in the shared DB, other tests' included
    assert b in woken and c not in woken

    rows = _line(root)
    assert [(r["agent_thread_id"], r["state"]) for r in rows] == [(b, "woken"), (c, "waiting")]
    assert rows[0]["wakes"] == 2 and rows[0]["wake_message_id"] != woken_b["wake_message_id"]
    assert 'Reply: "B survives"' in _wake(rows[0])["content"]


@pytest.mark.asyncio
async def test_an_agree_lands_at_once_while_a_person_holds_the_thread(
    integration_client, doc, _registrable_roots, driven,
):
    a, root = _comment_by_thread_a(integration_client, doc)
    page = object()
    await _frame(page, root, True)

    agreed = _patch(integration_client, _OWNER, root, acked=True, as_session_id=a)

    assert agreed.status_code == 200, agreed.text
    assert [ack["id"] for ack in agreed.json()["comment"]["acknowledged_by"]] == [a]
    assert _line(root) == []


def test_a_tag_wake_says_how_many_writers_are_in_line_on_the_thread(isolated_db):
    import uuid

    from doc_feedback_helpers import add_comment, seed_document, seed_thread
    from doceditor import comment_recipients, comments
    from infra.database import get_db
    from plans.feedback_nodes import item_blocks

    db = get_db()
    try:
        for name in ("owner", "tagged", "waiting-1", "waiting-2"):
            seed_thread(db, name, model=None)
        aid = seed_document(db, "owner")
        root, _ = add_comment(db, aid, author_kind="user", author_id=None)
        comment_recipients.save_tags(db, root, ["tagged"])
        loaded = comments.get_comment(root["id"], db=db)

        alone = {**loaded, "kind": "comment"}
        comment_recipients.prepare_notification(db, [alone], "tagged")
        for waiting in ("waiting-1", "waiting-2"):
            db.execute(
                """INSERT INTO doc_comment_writer_line(id,root_id,artifact_id,agent_thread_id,user_id,ops,saved_at)
                   VALUES (?,?,?,?,?,'[]',?)""",
                (uuid.uuid4().hex, root["id"], aid, waiting, "owner-token", f"2026-11-05T00:00:0{waiting[-1]}+00:00"))
        crowded = {**loaded, "kind": "comment"}
        comment_recipients.prepare_notification(db, [crowded], "tagged")
    finally:
        db.close()

    assert "line_hint" not in alone
    assert crowded["line_hint"] == "2 writers in line on this thread."
    assert "2 writers in line on this thread." in item_blocks({"author_kind": "user"}, [crowded])
