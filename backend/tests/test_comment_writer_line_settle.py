"""A saved row's end: it settles with the write that lands it, a line never waits on a lost serve or a dead agent."""

from __future__ import annotations

import json
import re
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


@pytest.mark.asyncio
async def test_post_as_is_returns_the_saved_text_of_any_failed_write_and_still_tries_the_rest(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    """A write that fails for any reason, not only a comment error, comes back with its text."""
    from doceditor import agent_comment_writes
    from tools.documents import DocCommentUpdateTool

    _a, root = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    page = object()
    await _frame(page, root, True)
    _post(integration_client, _OWNER, parent_id=root, body="B one", as_session_id=b)
    _post(integration_client, _OWNER, parent_id=root, body="B two", as_session_id=b)
    await _frame(page, root, False)
    real, calls = agent_comment_writes._replay, []

    async def first_fails(op, *args):
        calls.append(op)
        if len(calls) == 1:
            raise RuntimeError("the document's app loop is gone")
        return await real(op, *args)

    monkeypatch.setattr(agent_comment_writes, "_replay", first_fails)
    answer = json.loads(await DocCommentUpdateTool().execute(comment_id=root, post_as_is=True, _thread_id=b))

    [failed] = answer["failed"]
    assert failed["write"] == 'Reply: "B one"'
    assert re.fullmatch(r"It couldn't be posted \(reference \w{6}\)\. Write it again if it still applies\.",
                        failed["error"])
    assert [item["comment"]["body"] for item in answer["posted"]] == ["B two"]
    assert [reply["body"] for reply in _replies(root, b)] == ["B two"]


@pytest.mark.asyncio
async def test_a_line_that_fails_to_serve_neither_blocks_other_lines_nor_loses_the_sweeps_deliveries(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    from doceditor import feedback_batches, writer_line, writer_turns

    _a, first = _comment_by_thread_a(integration_client, doc)
    _a2, second = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="Agent B")
    c = _mk_thread(integration_client, _OWNER, title="Agent C")
    pages = object(), object()
    await _frame(pages[0], first, True)
    _post(integration_client, _OWNER, parent_id=first, body="B saved", as_session_id=b)
    await _frame(pages[1], second, True)
    _post(integration_client, _OWNER, parent_id=second, body="C saved", as_session_id=c)
    for page in pages:
        writer_turns.release(page)  # both lines left waiting, as after a lost serve
    real_text = writer_line.wake_text

    def broken_for_first(db, row):
        if row["root_id"] == first:
            raise KeyError("input")
        return real_text(db, row)

    monkeypatch.setattr(writer_line, "wake_text", broken_for_first)
    monkeypatch.setattr(feedback_batches, "close_due", lambda _now, db=None: ["delivered-thread"])

    drives = _sweep()

    assert "delivered-thread" in drives and c in drives and b not in drives
    assert _line(first) == []  # dropped with B told, not served again on every sweep
    assert any("an error stopped it (reference " in note and "B saved" in note for note in _notes(b))
    assert [r["state"] for r in _line(second)] == ["woken"]


@pytest.mark.asyncio
async def test_a_failing_line_step_at_turn_end_never_undoes_the_turns_own_settlement(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    from doceditor import writer_line

    root, b, _c = await _two_in_line(integration_client, doc)
    turn_id = _wake_turn(b, _line(root)[0])

    def broken(db, row, reason):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(writer_line, "_drop", broken)
    _end(b, turn_id, "completed")

    assert [r["status"] for r in _rows("SELECT status FROM active_turns WHERE id=?", turn_id)] == ["completed"]
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)][0] == (b, "woken")


@pytest.mark.asyncio
async def test_a_failing_line_step_at_turn_end_drops_that_row_with_its_agent_told_and_the_sweep_wakes_the_next(
    integration_client, doc, _registrable_roots, driven, monkeypatch,
):
    from doceditor import writer_line

    root, b, c = await _two_in_line(integration_client, doc)
    turn_id = _wake_turn(b, _line(root)[0])
    real_settle = writer_line._settle_ended

    def broken(db, row, turn):
        monkeypatch.setattr(writer_line, "_settle_ended", real_settle)
        raise KeyError("ops")

    monkeypatch.setattr(writer_line, "_settle_ended", broken)
    _end(b, turn_id, "completed")

    assert [r["agent_thread_id"] for r in _line(root)] == [c]
    assert any("an error stopped it (reference " in note and "B saved" in note for note in _notes(b))
    assert c in _sweep()
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]


@pytest.mark.asyncio
async def test_the_sweep_settles_a_woken_row_whose_turn_ended_with_no_turn_end_step(
    integration_client, doc, _registrable_roots, driven,
):
    """B's wake was read in a turn that is over, but nothing settled B's row: the sweep does."""
    root, b, c = await _two_in_line(integration_client, doc)
    _wake_turn(b, _line(root)[0], status="completed")

    assert c in _sweep()

    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]
    assert any("your turn ended without posting it as is or rewriting it" in note and "B saved" in note
               for note in _notes(b))


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_woken_row_whose_wake_is_queued_or_in_a_live_turn(
    integration_client, doc, _registrable_roots, driven,
):
    root, b, c = await _two_in_line(integration_client, doc)
    assert b not in _sweep() and c not in _sweep()  # B's wake is still queued
    _wake_turn(b, _line(root)[0])  # now read in a running turn
    assert c not in _sweep()
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken"), (c, "waiting")]


def test_a_line_step_that_lost_the_callers_transaction_is_raised_not_contained():
    """SQLite rolls back the whole transaction on some errors; carrying on would write outside it."""
    from doceditor import writer_line

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(x)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")

    def full_disk():
        db.rollback()  # what SQLite does itself on SQLITE_FULL inside a transaction
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(sqlite3.OperationalError):
        writer_line._contained(db, "root-1", full_disk)
    db.close()
