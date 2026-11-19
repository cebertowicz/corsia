"""The person's own writes move a handed-over document's pass where they are written.

A document an agent handed over has a passed version. Whatever path the person writes it by (live
typing, a version restore, an accepted suggestion), the next open serves their bytes, editable,
with no read and no passed version, on a document with no recorded history. A live patch made on
an agent's newer version that never passed is not the person's alone, so it still waits on a read.
"""
from __future__ import annotations

import asyncio

import pytest

from core import fresh_read_view
from delegation_fixtures import deleg_db  # noqa: F401
from test_fresh_read_gate import Reader, _reads

pytestmark = pytest.mark.fresh_read

PASSED = "Passed agent text.\n"
PERSON = "The person's own words.\n"


async def _handed_over(db, monkeypatch, name: str):
    """A registered document, with no history, whose current bytes passed a read; and the reader."""
    from infra import config
    from registry.service import register_write_strict

    path = config.WORKSPACE / "docs" / name  # a person's own folder: the shared/ category rules are not under test
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PASSED)
    artifact = register_write_strict("sender-self", str(path), db=db, notify=False)
    reader = Reader(monkeypatch, passes=True)
    await fresh_read_view.read_document(artifact["id"], path.name, path.read_bytes(), kind="document_open",
                                        writer_thread_id="sender-self", user_id="u-test")
    rechecks: list = []
    monkeypatch.setattr(fresh_read_view, "start_recheck", lambda *args: rechecks.append(args))
    return artifact, path, reader, rechecks


async def _opened(path) -> dict:
    from api.files_load import read_document

    return await asyncio.to_thread(read_document, str(path), "u-test", None, None)


def _live_publish(db, path, prior: str, text: str) -> None:
    """The person's live edit as the live publisher registers it: ``prior`` was on disk, ``text`` is now."""
    from doceditor.live_document import file_hash
    from doceditor.live_publication_record import Publication
    from doceditor.live_publisher import register_publication
    from doceditor.live_text import create

    path.write_text(text)
    publication = Publication("sender-self", None, None, create(prior).get_update(),
                              Publication.decision_for(path, None), direct_user=True)
    register_publication(publication, text, file_hash(text.encode()), stamp=None, db=db)


def _assert_served_as_written(opened: dict, reader: Reader, reads_before: int, rechecks: list) -> None:
    assert opened["text"] == PERSON
    assert opened["passed_version"] is None and opened["writable"] is True
    assert len(reader.items) == reads_before and rechecks == []  # no read spent on the person's words


@pytest.mark.asyncio
async def test_a_live_edit_by_the_person_is_served_as_written_with_no_read(deleg_db, monkeypatch):
    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "live.md")
    reads_before = len(reader.items)

    _live_publish(deleg_db, path, PASSED, PERSON)

    _assert_served_as_written(await _opened(path), reader, reads_before, rechecks)
    assert _reads(deleg_db)[-1]["verdict"] == "person_edit"


@pytest.mark.asyncio
async def test_a_run_of_the_persons_live_edits_is_one_record_holding_the_last_bytes(deleg_db, monkeypatch):
    from core.fresh_read_store import sha256_text, view

    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "typing.md")
    text = PASSED
    for word in ("one", "two", "three"):
        _live_publish(deleg_db, path, text, text + word + "\n")
        text += word + "\n"

    edits = [row for row in _reads(deleg_db) if row["verdict"] == "person_edit"]
    assert len(edits) == 1 and edits[0]["sha256"] == sha256_text(text)
    assert view(artifact["id"])["sha256"] == sha256_text(text)
    assert deleg_db.execute("SELECT COUNT(*) FROM fresh_read_bytes").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_live_edit_on_an_unchecked_agent_version_still_waits_on_a_read(deleg_db, monkeypatch):
    """Typing into an agent's newer, unread version keeps its words in the result, so it is not laundered."""
    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "patched.md")
    agent = "Agent text written from a shell.\n"
    path.write_text(agent)

    _live_publish(deleg_db, path, agent, PERSON + agent)

    opened = await _opened(path)
    assert opened["text"] == PASSED and opened["passed_version"]["notice"] == fresh_read_view.NOTICE_WAITING
    assert len(rechecks) == 1


@pytest.mark.asyncio
async def test_a_restore_by_the_person_is_served_as_written_with_no_read(deleg_db, monkeypatch):
    from api import files_history
    from doceditor import document_save

    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "restored.md")
    reads_before = len(reader.items)
    monkeypatch.setattr(files_history, "_restore_source",
                        lambda *_a: (PERSON, "Reason: Restored a version.", path.resolve()))

    async def recorded(path_, text, *_a, **_k):  # the history write itself is not under test here
        path_.write_text(text)
        return {"_notification": None, "version": "v2", "revision": "r"}

    monkeypatch.setattr(document_save, "_save_validated", recorded)

    result = await files_history.restore(str(path), "v1", "base", "u-test", None, None)

    assert result["version"] == "v2"
    _assert_served_as_written(await _opened(path), reader, reads_before, rechecks)


@pytest.mark.asyncio
async def test_an_accept_by_the_person_is_served_as_written_with_no_read(deleg_db, monkeypatch):
    """An accepted suggestion on the passed version applies checked words; the result is the person's."""
    from api.doc_comments import _person_writer

    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "accepted.md")
    reads_before = len(reader.items)
    saved: dict = {}

    await _person_writer({"id": "u-test"}, saved)(str(path), PERSON, None, "Accepted a suggestion.", None, None, ("s1",))

    assert saved["text"] == PERSON
    _assert_served_as_written(await _opened(path), reader, reads_before, rechecks)


@pytest.mark.asyncio
async def test_an_accept_on_an_unchecked_agent_version_starts_a_check_of_the_result(deleg_db, monkeypatch):
    """Design 4.4: an accepted suggestion applies to the current file and starts a check of the result."""
    from api.doc_comments import _person_writer

    artifact, path, reader, rechecks = await _handed_over(deleg_db, monkeypatch, "stale-accept.md")
    path.write_text("Agent text that never passed.\n")
    result = "Agent text that never passed, with the suggestion.\n"

    await _person_writer({"id": "u-test"}, {})(str(path), result, None, "Accepted a suggestion.", None, None, ("s1",))

    assert [args[2] for args in rechecks] == [result.encode()]
    assert _reads(deleg_db)[-1]["verdict"] == "passed"  # nothing recorded the result as the person's
