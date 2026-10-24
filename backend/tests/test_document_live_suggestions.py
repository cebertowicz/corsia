"""Accept runs against live text in the lane, including unsaved browser edits."""
import asyncio

import pytest

from doceditor import comments, comment_actions, comment_batch_actions
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


def suggestion(scope, entry):
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(entry.live.path),)).fetchone()[0]
    row, _ = comments.create_with_status(artifact, body="", author="author", author_kind="agent",
        author_thread_id=scope["owner"], anchor_text="b", from_off=2, to_off=3, replacement="B", db=scope["db"])
    entry.live.track_range(row["id"], row["from_off"], row["to_off"])
    return row


def typing(entry, start, delete=0, insert="USER "):
    client = clone(entry.live.doc.get_update())
    vector = client.get_state()
    if delete:
        del client["text"][start:start + delete]
    if insert:
        client["text"].insert(start, insert)
    entry.live.receive(BrowserMessage(f"typing-{vector.hex()}", client.get_update(vector)))


async def legacy_writer(*args):
    pytest.fail("Accept bypassed the live lane")


def user_id(scope):
    return scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_accept_checks_quotes_at_lane_start_after_queueing(ownership_scope, tmp_path, monkeypatch, batch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    second, _ = comments.create_with_status(row["artifact_id"], body="", author="author", author_kind="agent",
        author_thread_id=scope["owner"], anchor_text="second", from_off=4, to_off=10, replacement="SECOND", db=scope["db"])
    entry.live.track_range(second["id"], 4, 10)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    entry.lane.schedule_setup(hold)
    await entered.wait()
    decide = comment_batch_actions.decide_all_from_author if batch else comment_actions.decide_suggestion
    task = asyncio.create_task(decide(row["id"], "accept", user_id=user_id(scope),
        decided_by="user", actor_thread_id=None, write=legacy_writer))
    try:
        async def queued():
            while not entry.lane._queue:
                if task.done():
                    pytest.fail(f"Accept ended before queueing: {task.result()}")
                await asyncio.sleep(0)
        await asyncio.wait_for(queued(), 2)
        typing(entry, 2, delete=1, insert="changed")
        release.set()
        if batch:
            result = await task
            assert result["accepted"] == [second["id"]]
            assert [item["id"] for item in result["skipped"]] == [row["id"]]
            assert entry.live.text == entry.live.path.read_text() == "a changed\nSECOND\n"
            assert comments.get_comment(second["id"], db=scope["db"])["decision"] == "accepted"
        else:
            with pytest.raises(comment_actions.CommentActionError) as refused:
                await task
            assert refused.value.kind == "stale"
            assert entry.live.path.read_text() == "a b\nsecond\n"
        assert comments.get_comment(row["id"], db=scope["db"])["decision"] is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_accept_preserves_unsaved_browser_text(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    typing(entry, 0)
    try:
        decided = await comment_actions.decide_suggestion(row["id"], "accept", user_id=scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0],
            decided_by="user", actor_thread_id=None, write=legacy_writer)
        assert decided["decision"] == "accepted"
        assert "_live_receipt" not in decided
        assert entry.live.text == entry.live.path.read_text() == "USER a B\nsecond\n"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_accept_refuses_quote_changed_only_in_browser(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    entry.live.track_range(row["id"], 2, 3)
    typing(entry, 2, delete=1, insert="changed")
    before = entry.live.text
    try:
        with pytest.raises(comment_actions.CommentActionError) as refused:
            await comment_actions.decide_suggestion(row["id"], "accept", user_id=scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0],
                decided_by="user", actor_thread_id=None, write=legacy_writer)
        assert refused.value.kind == "stale"
        assert entry.live.text == before
        assert entry.live.path.read_text() == "a b\nsecond\n"
        assert comments.get_comment(row["id"], db=scope["db"])["decision"] is None
    finally:
        await manager.close()


def _edit_items(scope):
    return scope["db"].execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("bar_accept", [True, False])
async def test_the_bars_accept_in_the_live_lane_leaves_no_edit_feedback_but_a_plain_accept_does(
    ownership_scope, tmp_path, monkeypatch, bar_accept,
):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    try:
        result = await comment_batch_actions.accept_all_open(
            row["artifact_id"], user_id=user_id(scope), decided_by="user", actor_thread_id=None,
            write=legacy_writer) if bar_accept else await comment_batch_actions.decide_all_from_author(
            row["id"], "accept", user_id=user_id(scope), decided_by="user", actor_thread_id=None,
            write=legacy_writer)
        assert result["accepted"] == [row["id"]]
        await manager.close()
        assert _edit_items(scope) == (0 if bar_accept else 1)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_batch_accept_keeps_the_accepting_actor_as_writer(
    ownership_scope, tmp_path, monkeypatch,
):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    submit = entry.lane.submit

    async def capture_actor(operation):
        assert operation.actor.thread_id == scope["owner"]
        assert operation.actor.user_id == user_id(scope)
        return await submit(operation)

    monkeypatch.setattr(entry.lane, "submit", capture_actor)
    try:
        result = await comment_batch_actions.accept_all_open(
            row["artifact_id"], user_id=user_id(scope), decided_by="user", actor_thread_id=scope["owner"],
            write=legacy_writer,
        )
        assert result["accepted"] == [row["id"]]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_the_bars_flush_publishes_the_persons_typing_before_the_accept_so_it_stays_feedback(
    ownership_scope, tmp_path, monkeypatch,
):
    """The bar flushes before accepting: typing and accept never share a publication."""
    from doceditor.live_operation import Operation
    from doceditor.live_publication_record import WriteActor
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    typing(entry, 0)
    try:
        person = WriteActor("ui", None, direct_user=True, user_id=user_id(scope))
        await entry.live.commit_received()
        (await entry.lane.submit(Operation("flush", actor=person))).require_landed()
        result = await comment_batch_actions.accept_all_open(
            row["artifact_id"], user_id=user_id(scope), decided_by="user", actor_thread_id=None,
            write=legacy_writer)
        assert result["accepted"] == [row["id"]]
        await manager.close()
        assert entry.live.path.read_text() == "USER a B\nsecond\n"
        items = scope["db"].execute("SELECT latest_text, save_count FROM doc_feedback_edit_items").fetchall()
        assert len(items) == 1
        assert "USER" in items[0]["latest_text"]
    finally:
        await manager.close()
