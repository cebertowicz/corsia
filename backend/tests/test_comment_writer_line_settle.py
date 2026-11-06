"""A saved row's end: it settles with the write that lands it, a line never waits on a lost serve or a dead agent."""

from __future__ import annotations

import json
import sqlite3

import pytest

from delegation_docs_fixtures import _OWNER, _comment_by_thread_a, _mk_thread, _post, _registrable_roots, doc
from test_comment_writer_turn import _end, _fresh_turns, _frame, _line, _replies, _rows, _wake_turn, driven

pytestmark = pytest.mark.integration

__all__ = ["_fresh_turns", "_registrable_roots", "doc", "driven"]


def _sweep() -> list[str]:
    from automation.continuation.sources.feedback_batches import FeedbackBatchSource
    from datetime import datetime, timezone

    return FeedbackBatchSource().prepare(datetime.now(timezone.utc).isoformat(), db=None)


def _execute(sql: str, *args) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="test_writer_line") as connection:
            connection.execute(sql, args)
    finally:
        db.close()


def _notes(thread_id: str) -> list[str]:
    return [row["summary"] for row in _rows(
        "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", thread_id)]


async def _two_in_line(client, doc):
    """B woken (its wake not yet read), C waiting behind it."""
    _a, root = _comment_by_thread_a(client, doc)
    b = _mk_thread(client, _OWNER, title="Agent B")
    c = _mk_thread(client, _OWNER, title="Agent C")
    page = object()
    await _frame(page, root, True)
    _post(client, _OWNER, parent_id=root, body="B saved", as_session_id=b)
    _post(client, _OWNER, parent_id=root, body="C saved", as_session_id=c)
    await _frame(page, root, False)
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken"), (c, "waiting")]
    return root, b, c


@pytest.mark.asyncio
async def test_a_rewrite_whose_settle_fails_is_not_posted_and_stays_in_line(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    """The settle is part of the comment write: when it fails, the reply rolls back with it."""
    from doceditor import writer_line, writer_turns
    from tools.documents import DocCommentCreateTool

    root, b, _c = await _two_in_line(integration_client, doc)

    def broken(db, row, outcome):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(writer_line, "settle", broken)
    answer = await DocCommentCreateTool().execute(body="B, rewritten", parent_id=root, _thread_id=b)

    assert answer == "Error creating document comment: disk I/O error"
    assert _replies(root, b) == []
    assert [r["agent_thread_id"] for r in _line(root)][0] == b
    assert writer_turns.agent_writing(root) is None


@pytest.mark.asyncio
async def test_post_as_is_after_its_row_is_gone_is_refused_and_saves_nothing(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    """The saved row is read in the admitting transaction: a row dropped just before is never re-created empty."""
    from doceditor import agent_comment_writes
    from tools.documents import DocCommentUpdateTool

    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    page = object()
    await _frame(page, root, True)
    _post(integration_client, _OWNER, parent_id=root, body="B saved", as_session_id=b)
    real = agent_comment_writes.run_in_db

    async def dropped_first(fn, *args, **kwargs):
        if fn.__name__.startswith("_admit"):
            _execute("DELETE FROM doc_comment_writer_line WHERE root_id=?", root)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(agent_comment_writes, "run_in_db", dropped_first)
    answer = await DocCommentUpdateTool().execute(comment_id=root, post_as_is=True, _thread_id=b)

    assert answer == "Error: You have nothing saved in line on this thread."
    assert _line(root) == []


@pytest.mark.asyncio
async def test_the_continuation_sweep_serves_a_line_a_lost_serve_left_waiting(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import writer_turns

    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    page = object()
    await _frame(page, root, True)
    _post(integration_client, _OWNER, parent_id=root, body="B saved", as_session_id=b)
    writer_turns.release(page)  # the person left, but the serve that should follow never ran

    assert [r["state"] for r in _line(root)] == ["waiting"]
    assert b in _sweep()
    assert [r["state"] for r in _line(root)] == ["woken"]
    assert _sweep().count(b) == 0  # a woken agent is not woken twice


@pytest.mark.asyncio
async def test_a_failed_serve_after_a_person_leaves_is_logged_as_an_error(monkeypatch):
    from doceditor import agent_comment_writes
    from ws import comment_turn

    logged: list[str] = []

    async def failing(_roots):
        raise RuntimeError("database is locked")

    class Recorder:
        def exception(self, message, *args):
            logged.append(message.format(*args))

    monkeypatch.setattr(agent_comment_writes, "serve_freed", failing)
    monkeypatch.setattr(comment_turn, "logger", Recorder())
    await comment_turn._serve(["root-1"])

    assert logged == ["comment_turn: serving ['root-1'] failed; the continuation sweep serves it"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["wake_failed", "model_dead"])
async def test_a_woken_agent_that_cannot_take_input_drops_out_is_told_and_the_next_is_woken(
    integration_client, doc, _registrable_roots, driven, monkeypatch, cause,
):
    from infra import send_target

    root, b, c = await _two_in_line(integration_client, doc)
    if cause == "wake_failed":
        _execute("UPDATE messages SET delivery_state='failed' WHERE id=?", _line(root)[0]["wake_message_id"])
        reason = "your thread could not take the wake"
    else:
        _execute("UPDATE threads SET model='native/gone' WHERE id=?", b)
        monkeypatch.setattr(send_target, "model_admission",
                            lambda model, **_kwargs: "fail" if model == "native/gone" else "run")
        reason = "your thread's model cannot take input"

    assert c in _sweep()

    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]
    assert any(reason in note and "B saved" in note for note in _notes(b))
    assert _replies(root, b) == []


@pytest.mark.asyncio
async def test_a_queued_wake_whose_thread_cannot_take_it_drops_at_that_threads_turn_end(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    """Another turn of B's ends with the wake still queued; B's model is gone, so B leaves the line and C goes."""
    from infra import send_target

    root, b, c = await _two_in_line(integration_client, doc)
    woken_b = _line(root)[0]
    _execute("UPDATE threads SET model='native/gone' WHERE id=?", b)
    monkeypatch.setattr(send_target, "model_admission",
                        lambda model, **_kwargs: "fail" if model == "native/gone" else "run")
    other = dict(woken_b, id="other", wakes=0, wake_message_id="not-the-wake")

    settlement = _end(b, _wake_turn(b, other, read=False), "completed")

    assert [r["delivery_state"] for r in _rows("SELECT delivery_state FROM messages WHERE id=?",
                                                woken_b["wake_message_id"])] == ["queued"]
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]
    assert c in settlement.wake_thread_ids


@pytest.mark.asyncio
async def test_each_line_step_records_one_product_event(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    from doceditor import live_events
    from tools.documents import DocCommentUpdateTool

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(live_events, "emit", lambda action, **props: events.append((action, props["outcome"], props)))
    _a, root = _comment_by_thread_a(integration_client, doc)
    b, c, d = (_mk_thread(integration_client, _OWNER, title=f"Agent {name}") for name in "BCD")
    page = object()
    await _frame(page, root, True)
    for agent in (b, c, d):
        _post(integration_client, _OWNER, parent_id=root, body=f"saved by {agent}", as_session_id=agent)
    await _frame(page, root, False)
    json.loads(await DocCommentUpdateTool().execute(comment_id=root, post_as_is=True, _thread_id=b))
    _end(c, _wake_turn(c, _line(root)[0]), "completed")
    _post(integration_client, _OWNER, parent_id=root, body="D, rewritten", as_session_id=d)

    steps = [outcome for action, outcome, _props in events if action == "comment_writer_line"]
    assert steps == ["queued", "queued", "queued", "woken", "posted_as_is", "woken", "dropped", "woken", "rewritten"]
    for _action, _outcome, props in events:
        assert set(props) <= {"outcome", "root_id", "artifact_id", "line_id", "wakes", "waited_seconds"}
