"""The fresh read before the person: each door refuses and delivers nothing unless a read passed.

The reader seam (``core.fresh_read._read_once``) is replaced; the rest of the gate runs: the
candidate order, the record, the product event and each door's refusal.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from core import fresh_read
from core.fresh_read_candidates import Candidate
from core.fresh_read_prompt import ReaderAnswer
from core.fresh_read_store import sha256_bytes, sha256_text
from delegation_fixtures import deleg_db  # noqa: F401
from infra.thread_input_types import TurnIdentity

pytestmark = pytest.mark.fresh_read

TURN = TurnIdentity(turn_id="turn-1", turn_order=1)


def _candidate(model: str, *, own: bool = False) -> Candidate:
    return Candidate(model=model, window=200_000, output_allowance=2_000, long_context_bound=None,
                     server_id=None, is_local=False, writer_own=own)


class Reader:
    """A reader that passes, or fails every item with one finding quoting its first words."""

    def __init__(self, monkeypatch, *, passes: bool, models: tuple[str, ...] = ("reader/cheap",)):
        self.passes = passes
        self.items: list[tuple[str, fresh_read.Item, str]] = []
        monkeypatch.setattr(fresh_read, "candidates",
                            lambda _writer: [_candidate(model, own=i == len(models) - 1) for i, model in enumerate(models)])
        monkeypatch.setattr(fresh_read, "_read_once", self._read)

    async def _read(self, candidate, item, prompt):
        self.items.append((candidate.model, item, prompt))
        if self.passes:
            return ReaderAnswer(True, ()), None
        finding = {"check": 1, "quote": item.shown[:12], "why": "The person cannot act on this."}
        return ReaderAnswer(False, (finding,)), None


def _reads(db) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM fresh_reads ORDER BY created_at")]


def _updates(db) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT content, metadata FROM messages WHERE thread_id='sender-self'")]


@pytest.fixture
def quiet_broadcast(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("tools.post_update.broadcast_message_rows", AsyncMock())


@pytest.mark.asyncio
async def test_update_refused_and_nothing_written_when_the_reader_fails(deleg_db, monkeypatch, quiet_broadcast):
    from tools.post_update import PostUpdateTool

    Reader(monkeypatch, passes=False)
    result = await PostUpdateTool().execute(_thread_id="sender-self", text="Shipped the fix yesterday.",
                                            _host_access_turn_identity=TURN)

    assert result.startswith("Error: Not delivered") and '"Shipped the "' in result
    assert _updates(deleg_db) == []
    assert [row["verdict"] for row in _reads(deleg_db)] == ["failed"]


@pytest.mark.asyncio
async def test_no_reader_holds_the_item_with_each_reason_within_the_bound(deleg_db, monkeypatch, quiet_broadcast):
    """Every candidate unavailable: the update is refused naming why, after at most one timeout each."""
    from tools.post_update import PostUpdateTool

    monkeypatch.setattr(fresh_read, "READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(fresh_read, "candidates",
                        lambda _writer: [_candidate("reader/gone"), _candidate("reader/slow", own=True)])

    def resolve(candidate, _writer):
        if candidate.model == "reader/gone":
            raise LookupError("model_unavailable")
        return type("Target", (), {"account_id": None})(), None

    async def hangs(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(fresh_read, "_resolve", resolve)
    monkeypatch.setattr("llm.isolated_structured.invoke_isolated_structured", hangs)
    started = time.monotonic()
    result = await PostUpdateTool().execute(_thread_id="sender-self", text="Done.", _host_access_turn_identity=TURN)

    assert time.monotonic() - started < 2
    assert "could not run" in result
    assert "reader/gone: not available (model_unavailable)" in result and "reader/slow: timed out" in result
    assert _updates(deleg_db) == []
    assert [row["verdict"] for row in _reads(deleg_db)] == ["held"]


@pytest.mark.asyncio
async def test_a_pass_binds_the_shown_text_an_edit_reads_again_and_a_replay_does_not(
        deleg_db, monkeypatch, quiet_broadcast):
    from tools.post_update import PostUpdateTool

    reader = Reader(monkeypatch, passes=True)
    tool = PostUpdateTool()
    first = await tool.execute(_thread_id="sender-self", text="  The check passed.  ", idempotency_key="k1",
                               _host_access_turn_identity=TURN)
    assert first.startswith("Update saved as message ")
    (read,) = _reads(deleg_db)
    assert read["sha256"] == sha256_text("The check passed.") and read["verdict"] == "passed"
    assert json.loads(_updates(deleg_db)[0]["metadata"])["fresh_read_id"] == read["id"]

    await tool.execute(_thread_id="sender-self", text="The check passed twice.", _host_access_turn_identity=TURN)
    assert len(reader.items) == 2  # new text, new read

    replay = await tool.execute(_thread_id="sender-self", text="The check passed.", idempotency_key="k1",
                                _host_access_turn_identity=TURN)
    assert replay.splitlines()[0] == first.splitlines()[0]
    assert len(reader.items) == 2  # the replay delivers nothing new and needs no read


@pytest.mark.asyncio
async def test_the_product_event_carries_the_verdict(deleg_db, monkeypatch, quiet_broadcast):
    from tools.post_update import PostUpdateTool

    events = []
    monkeypatch.setattr("infra.product_events.emit_product_event", lambda *a, **k: events.append((a, k)))
    Reader(monkeypatch, passes=True)
    await PostUpdateTool().execute(_thread_id="sender-self", text="Ready.", _host_access_turn_identity=TURN)

    (args, kwargs), = [event for event in events if event[0][0] == "fresh_read"]
    assert args == ("fresh_read", "passed")
    assert kwargs["properties"]["kind"] == "update" and kwargs["properties"]["reader_model"] == "reader/cheap"


def _registered(db, name: str, text: str) -> tuple[dict, object]:
    from infra import config
    from registry.service import register_write_strict

    path = config.WORKSPACE / "shared" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return register_write_strict("sender-self", str(path), db=db, notify=False), path


def _viewing(monkeypatch) -> list:
    from infra import presence, state

    sent: list = []
    monkeypatch.setattr(presence, "document_open_snapshot",
                        lambda *_a: presence.DocumentOpenSnapshot("here", frozenset({"session-a"}), False))
    def send(_thread, _sessions, message) -> int:
        sent.append(message)
        return 1

    monkeypatch.setattr(state, "send_to_thread_sessions", send)
    return sent


@pytest.mark.asyncio
async def test_document_open_refused_and_not_sent_when_the_reader_fails(deleg_db, monkeypatch):
    from tools.document_open import DocumentOpenTool

    artifact, path = _registered(deleg_db, "plan.md", "# Plan\nAll done.\n")
    sent = _viewing(monkeypatch)
    Reader(monkeypatch, passes=False)

    result = json.loads(await DocumentOpenTool().execute(path=str(path), _thread_id="sender-self"))

    assert result["outcome"] == "refused" and result["reason"] == "not_delivered"
    assert "Not delivered" in result["message"]
    assert sent == []


@pytest.mark.asyncio
async def test_a_shell_edit_after_a_pass_serves_the_passed_version(deleg_db, monkeypatch):
    """An agent's later write by any path is not what the person sees until a read passes it."""
    from core import fresh_read_view
    from tools.document_open import DocumentOpenTool

    artifact, path = _registered(deleg_db, "brief.md", "# Brief\nVersion one.\n")
    sent = _viewing(monkeypatch)
    Reader(monkeypatch, passes=True)
    result = json.loads(await DocumentOpenTool().execute(path=str(path), _thread_id="sender-self"))
    assert result["outcome"] == "requested" and sent[0]["fresh_read_id"]

    path.write_text("# Brief\nVersion two, written from a shell.\n")
    rechecks = []
    monkeypatch.setattr(fresh_read_view, "_start_recheck", lambda *args: rechecks.append(args))
    shown = await asyncio.to_thread(fresh_read_view.served, artifact, path, path.read_bytes(), True)

    assert shown["text"] == "# Brief\nVersion one.\n"
    assert shown["writable"] is False
    assert shown["passed_version"]["notice"] == fresh_read_view.NOTICE_WAITING
    assert len(rechecks) == 1
    assert path.read_text() == "# Brief\nVersion two, written from a shell.\n"  # opening writes nothing


@pytest.mark.asyncio
async def test_a_failed_recheck_opens_the_checked_copy_editable(deleg_db, monkeypatch):
    from core import fresh_read_view

    artifact, path = _registered(deleg_db, "notes.md", "First.\n")
    Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    path.write_text("Second.\n")
    Reader(monkeypatch, passes=False)
    await fresh_read_view._recheck(artifact["id"], path, path.read_bytes(), "sender-self", None)

    shown = await asyncio.to_thread(fresh_read_view.served, artifact, path, path.read_bytes(), True)
    assert shown["text"] == "First.\n" and shown["writable"] is True
    assert shown["passed_version"]["notice"] == fresh_read_view.NOTICE_FAILED


@pytest.mark.asyncio
async def test_a_passing_recheck_moves_the_view_to_the_new_version(deleg_db, monkeypatch):
    from core import fresh_read_view

    artifact, path = _registered(deleg_db, "status.md", "One.\n")
    Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    path.write_text("Two.\n")
    await fresh_read_view._recheck(artifact["id"], path, path.read_bytes(), "sender-self", None)

    assert await asyncio.to_thread(fresh_read_view.served, artifact, path, path.read_bytes(), True) is None


@pytest.mark.asyncio
async def test_the_persons_save_over_a_failed_version_keeps_it_aside_and_wakes_the_holder_once(
        deleg_db, monkeypatch, tmp_path):
    """History off: the agent's replaced bytes go to its scratch folder, and the wake names the path."""
    from core import fresh_read_person_save, fresh_read_view
    from core.fresh_read_store import view

    artifact, path = _registered(deleg_db, "draft.md", "Passed.\n")
    Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    path.write_text("Failed agent text.\n")
    monkeypatch.setattr("infra.guidance_targets.history_target", lambda *_a: None)
    monkeypatch.setattr("core.thread_lifecycle_paths.thread_dir", lambda *_a: tmp_path / "holder")
    wakes = []

    async def deliver(thread, kind, summary, payload, *, delivery_id):
        wakes.append((thread, kind, summary, payload, delivery_id))

    monkeypatch.setattr("events.delivery.deliver_to_thread", deliver)
    expected = []

    async def save(sha):
        expected.append(sha)
        path.write_text("The person's edit.\n")
        return {"ok": True}

    assert await fresh_read_person_save.person_save(str(path), save) == {"ok": True}

    # The save is checked against the agent bytes it replaces, so a newer write in between is not lost.
    assert expected == [sha256_bytes(b"Failed agent text.\n")]
    (thread, kind, summary, payload, _delivery), = wakes
    assert thread == "sender-self" and kind == "fresh_read.saved_over"
    scratch = payload["scratch_path"]
    assert scratch and open(scratch).read() == "Failed agent text.\n" and scratch in summary
    row = await asyncio.to_thread(view, artifact["id"])
    assert bytes(row["body"]) == b"The person's edit.\n"
    assert _reads(deleg_db)[-1]["verdict"] == "person_edit"


def _comment(**overrides):
    from doceditor.comment_create_db import CommentInput

    fields = dict(artifact_id=None, path=None, body="Please confirm the date.", author="Agent",
                  author_kind="agent", user_id="u-test", author_thread_id="sender-self", tag_person=True)
    fields.update(overrides)
    return CommentInput(**fields)


@pytest.mark.asyncio
async def test_an_agent_comment_for_the_person_is_refused_and_not_created_when_the_reader_fails(
        deleg_db, monkeypatch):
    from doceditor import comment_creation
    from doceditor.comment_action_errors import CommentActionError

    created = []

    async def owned(**kwargs):
        created.append(kwargs)

    monkeypatch.setattr(comment_creation, "create_comment_owned", owned)
    Reader(monkeypatch, passes=False)

    with pytest.raises(CommentActionError) as refused:
        await comment_creation.create_validated_comment(_comment(), None)

    assert refused.value.kind == "not_delivered" and "Not delivered" in refused.value.message
    assert created == []


@pytest.mark.asyncio
async def test_a_comment_that_reaches_only_agents_or_replays_needs_no_read(deleg_db, monkeypatch):
    from doceditor import comment_creation

    created = []

    async def owned(**kwargs):
        created.append(kwargs)
        return ({"id": kwargs["client_id"]},)

    monkeypatch.setattr(comment_creation, "create_comment_owned", owned)
    reader = Reader(monkeypatch, passes=False)

    await comment_creation.create_validated_comment(_comment(tag_person=False), None)  # no document, no tag
    deleg_db.execute(
        "INSERT INTO doc_comments (id, artifact_id, author, author_kind, body, created_at, updated_at) "
        "VALUES ('kept', 'a1', 'Agent', 'agent', 'Please confirm the date.', 't', 't')")
    deleg_db.commit()
    await comment_creation.create_validated_comment(_comment(), "kept")  # a retry of a stored comment

    assert reader.items == [] and len(created) == 2


@pytest.mark.asyncio
async def test_the_persons_own_comment_is_never_read(deleg_db, monkeypatch):
    """Read-only on a stale document is text edits only: the person's comments stay open."""
    from doceditor import comment_creation

    created = []

    async def owned(**kwargs):
        created.append(kwargs)
        return ({"id": "c"},)

    monkeypatch.setattr(comment_creation, "create_comment_owned", owned)
    monkeypatch.setattr("jobs.corrections_recording.notify_doc_comment", lambda _id: None)
    reader = Reader(monkeypatch, passes=False)

    await comment_creation.create_validated_comment(_comment(author_kind="user", author="ui", tag_person=False), None)

    assert reader.items == [] and len(created) == 1


@pytest.mark.asyncio
async def test_an_agent_edit_of_its_comment_for_the_person_is_refused_and_not_written(deleg_db, monkeypatch):
    from doceditor import agent_comment_writes
    from doceditor.comment_action_errors import CommentActionError

    deleg_db.execute(
        "INSERT INTO doc_comments (id, artifact_id, author, author_kind, author_thread_id, body, anchor_text, "
        "tagged_user_id, created_at, updated_at) VALUES ('c1', 'a1', 'Agent', 'agent', 'sender-self', "
        "'Old words.', 'the passage', 'u-test', 't', 't')")
    deleg_db.commit()
    written = []
    monkeypatch.setattr(agent_comment_writes, "_update_in_db", lambda *args: written.append(args))
    reader = Reader(monkeypatch, passes=False)

    with pytest.raises(CommentActionError) as refused:
        await agent_comment_writes._gated_update("c1", {"body": "New words."}, "sender-self", "u-test", True)

    assert refused.value.kind == "not_delivered" and written == []
    (_model, item, prompt), = reader.items
    assert item.shown == "New words." and "the passage" in prompt

    reader.passes = True
    await agent_comment_writes._gated_update("c1", {"resolved": True}, "sender-self", "u-test", True)
    assert len(reader.items) == 1 and len(written) == 1  # no change to its text, no read


@pytest.mark.asyncio
async def test_the_document_read_serves_the_passed_text_read_only(deleg_db, monkeypatch):
    """The HTTP open shows the passed version, read-only, once an agent's newer bytes land."""
    from api.files_load import read_document
    from core import fresh_read_view

    artifact, path = _registered(deleg_db, "served.md", "Checked.\n")
    Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    monkeypatch.setattr(fresh_read_view, "_start_recheck", lambda *args: None)
    fresh = await asyncio.to_thread(read_document, str(path), "u-test", None, None)
    assert fresh["text"] == "Checked.\n" and fresh["passed_version"] is None

    path.write_text("Unchecked.\n")
    stale = await asyncio.to_thread(read_document, str(path), "u-test", None, None)

    assert stale["text"] == "Checked.\n" and stale["writable"] is False
    assert stale["passed_version"]["current_sha256"] == sha256_bytes(b"Unchecked.\n")


@pytest.mark.asyncio
async def test_the_live_open_answers_non_live_with_the_passed_version(monkeypatch):
    """A live session would sync to the file's newer bytes, so the socket open never starts one."""
    from ws import document_control

    passed = {"sha256": "a", "fresh_read_id": "r", "current_sha256": "b", "notice": "n"}
    opened, sent = [], []

    class Manager:
        async def open(self, *args):
            opened.append(args)

    async def send(_socket, message):
        sent.append(message)

    monkeypatch.setattr(document_control, "get_documents", lambda: Manager())
    monkeypatch.setattr(document_control, "read_document",
                        lambda *_a, **_k: {"artifact": {"id": "doc", "path": "/w/doc.md"}, "passed_version": passed})
    monkeypatch.setattr(document_control, "send_to_client", send)

    await document_control._open(object(), {"id": "u-test"}, {"path": "/w/doc.md", "request_id": "q"})

    assert sent == [{"type": "doc_opened", "live": False, "passed_version": passed, "request_id": "q"}]
    assert opened == []
