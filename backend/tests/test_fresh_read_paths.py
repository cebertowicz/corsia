"""The fresh read's less travelled paths: holds, the holder's wake, what is read, and refusals first.

Each case runs the real gate with the reader seam replaced (``test_fresh_read_gate.Reader``), so
what is under test is what the gate gathers, records and tells, not a model's judgment.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from core import fresh_read, fresh_read_view
from core.fresh_read import Item
from delegation_fixtures import deleg_db  # noqa: F401
from test_fresh_read_gate import Reader, _reads, _registered

pytestmark = pytest.mark.fresh_read


async def _passed(db, monkeypatch, name: str, text: str):
    artifact, path = _registered(db, name, text)
    Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    return artifact, path


def _no_reader(monkeypatch) -> None:
    monkeypatch.setattr(fresh_read, "candidates", lambda _writer: [])


@pytest.mark.asyncio
async def test_a_held_recheck_decides_nothing_and_the_next_open_reads_again(deleg_db, monkeypatch):
    artifact, path = await _passed(deleg_db, monkeypatch, "held.md", "One.\n")
    path.write_text("Two.\n")
    _no_reader(monkeypatch)
    await fresh_read_view._recheck(artifact["id"], path, path.read_bytes(), "sender-self", None)
    assert _reads(deleg_db)[-1]["verdict"] == "held"

    rechecks: list = []
    monkeypatch.setattr(fresh_read_view, "start_recheck", lambda *args: rechecks.append(args))
    shown = await asyncio.to_thread(fresh_read_view.served, artifact, path, path.read_bytes(), True)

    assert shown["text"] == "One.\n" and shown["writable"] is False
    assert shown["passed_version"]["notice"] == fresh_read_view.NOTICE_WAITING
    assert len(rechecks) == 1


@pytest.mark.asyncio
async def test_a_failed_recheck_wakes_the_holder_once_with_the_findings_only(deleg_db, monkeypatch):
    artifact, path = await _passed(deleg_db, monkeypatch, "wake.md", "One.\n")
    path.write_text("Two, worse.\n")
    woken: list = []

    async def deliver(thread_id, kind, summary, payload, *, delivery_id):
        woken.append((thread_id, kind, summary, payload, delivery_id))

    monkeypatch.setattr("events.delivery.deliver_to_thread", deliver)
    Reader(monkeypatch, passes=False)
    await fresh_read_view._recheck(artifact["id"], path, path.read_bytes(), "sender-self", None)

    (thread_id, kind, summary, payload, delivery_id), = woken
    assert thread_id == "sender-self" and kind == "fresh_read.recheck_failed"
    assert '"Two, worse.' in summary and "The person cannot act on this." in summary  # a quote may span lines
    assert "call" not in summary.lower()  # the door's retry advice does not apply to a background read
    assert payload["artifact_id"] == artifact["id"] and delivery_id.endswith(payload["sha256"])


@pytest.mark.asyncio
async def test_no_input_to_read_against_holds_the_item(deleg_db, monkeypatch):
    reader = Reader(monkeypatch, passes=True)

    def broken(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fresh_read, "_task", broken)
    verdict = await fresh_read.check(Item(kind="update", item_ref="sender-self", shown="Done.",
                                          writer_thread_id="sender-self", user_id="u-test", node_id="n1"))

    assert verdict.held and not verdict.passed and reader.items == []
    assert "could not read the task or the person's messages (OperationalError)" in verdict.message
    assert _reads(deleg_db)[-1]["verdict"] == "held"


def test_the_persons_messages_stop_once_a_chat_passes_the_cap():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE messages (thread_id, role, content, metadata, created_at)")
    db.execute("CREATE TABLE threads (id, parent_thread_id)")
    db.execute("INSERT INTO threads VALUES ('child', 'parent'), ('parent', NULL)")
    for thread_id in ("child", "parent"):
        db.executemany("INSERT INTO messages VALUES (?, 'user', ?, NULL, ?)",
                       [(thread_id, f"{thread_id} message {i}", f"2026-11-19T00:{i:02d}") for i in range(50)])
    measured: list[str] = []

    def measure(line: str) -> int:
        measured.append(line)
        return 10

    found = fresh_read._person_messages(db, "child", 25, measure)

    assert len(measured) == 6  # three per chat: the third passes the cap of 25
    assert [text for _at, text in found][:2] == ["child message 49", "parent message 49"]
    assert fresh_read._person_messages(db, "child", None, measure) == ()


@pytest.mark.asyncio
async def test_an_update_reads_its_unregistered_card_and_not_a_file_outside_scope(
        deleg_db, monkeypatch, tmp_path_factory):
    from infra import config
    from tools.post_update_fresh_read import read_update

    loose = config.WORKSPACE / "docs" / "loose.md"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text("Loose notes the person can open.\n")
    outside = tmp_path_factory.mktemp("elsewhere") / "private.md"
    outside.write_text("Not this install's to show.\n")
    assert config.WORKSPACE.resolve() not in outside.resolve().parents
    reader = Reader(monkeypatch, passes=True)

    verdict = await read_update("sender-self", f"Read [notes]({loose}) and [that]({outside}).", None, None, None)

    assert verdict.passed
    (_model, item, prompt), = reader.items
    assert [(doc.artifact_id, doc.name) for doc in item.documents] == [(None, "loose.md")]
    assert "Loose notes the person can open." in prompt and "Not this install's" not in prompt
    assert _reads(deleg_db)[-1]["verdict"] == "passed"  # read, with no view to move


@pytest.mark.asyncio
async def test_a_refused_comment_edit_is_refused_as_itself_before_any_read(deleg_db, monkeypatch):
    from doceditor import agent_comment_writes
    from doceditor.comment_action_errors import CommentActionError

    artifact, _path = _registered(deleg_db, "commented.md", "Text.\n")
    deleg_db.execute(
        "INSERT INTO doc_comments (id, artifact_id, author, author_kind, author_thread_id, body, anchor_text, "
        "tagged_user_id, created_at, updated_at) VALUES ('c2', ?, 'Agent', 'agent', 'other-agent', "
        "'Their words.', 'Text', 'u-test', 't', 't')", (artifact["id"],))
    deleg_db.commit()
    reader = Reader(monkeypatch, passes=True)

    with pytest.raises(CommentActionError) as refused:
        await agent_comment_writes._gated_update("c2", {"body": "My words."}, "sender-self", "u-test", True)

    assert refused.value.kind == "not_author" and reader.items == []
    assert await agent_comment_writes._gated_update("missing", {"body": "x"}, "sender-self", "u-test", True) is None
    assert reader.items == []
