"""DCOL-FR9: what leaves the server about a viewing note, and what is measured."""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from tests.test_document_viewing_context import KEY, SCOPE_LINE, _admit, viewed_doc  # noqa: F401

TARGET_KEY = "viewing_document_target"


def test_the_live_frame_carries_neither_the_client_value_nor_the_note():
    from infra.send_broadcast import InputBroadcastRow, project_input_message

    # The caller's dict at broadcast time still holds what the client sent.
    client = {KEY: {"artifact_id": "a1", "heading_anchor": 3, "path": "forged.md"},
              TARGET_KEY: {"artifact_id": "a1"}, "attachments": ["f1"]}
    pushed = project_input_message(InputBroadcastRow(
        message_id="m1", content="c", metadata=client, created_at="t",
        role="user", delivery_intent=None,
    ))

    assert pushed["metadata"] == {"attachments": ["f1"]}
    # The caller's own dict is left as it was.
    assert KEY in client and TARGET_KEY in client


async def _send(db, input_id, target):
    from infra.send_service import send_thread_input

    thread = dict(db.execute("SELECT * FROM threads WHERE id='sender-self'").fetchone())
    sent = await send_thread_input(
        "sender-self", thread, "Review this section", native_mode="ws", ws=AsyncMock(),
        delivery_intent="queue_after_turn", input_id=input_id, metadata={KEY: target},
    )
    await _reports_written()
    return sent


async def _reports_written():
    from infra import input_preparation

    # The send does not wait for its report, so the test does.
    await asyncio.gather(*input_preparation._pending_reports)


def _outcomes(mock_emit):
    return [
        (event, thread_id, properties) for event, thread_id, properties in mock_emit.full
        if event == "document.viewing_note"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor,outcome", [(3, "section"), (4, "path_only"), (None, "path_only")])
async def test_an_admitted_send_reports_how_far_its_note_resolved(
    deleg_db, viewed_doc, mock_emit, anchor, outcome,
):
    _path, artifact_id = viewed_doc
    _admit(deleg_db, "busy")
    # Line 4 is blank: an editor showing the heading line there does not match it.
    target = {"artifact_id": artifact_id,
              **({"heading_anchor": anchor, "heading_line": SCOPE_LINE} if anchor else {})}

    await _send(deleg_db, "m-fresh", target)

    assert _outcomes(mock_emit) == [
        ("document.viewing_note", "sender-self", {"outcome": outcome, "contract_version": 1}),
    ]


@pytest.mark.asyncio
async def test_an_inaccessible_document_reports_once_and_a_retry_reports_nothing(
    deleg_db, viewed_doc, mock_emit,
):
    _path, artifact_id = viewed_doc
    _admit(deleg_db, "busy")
    await _send(deleg_db, "m-none", {"artifact_id": "no-such-artifact"})
    assert [event[2]["outcome"] for event in _outcomes(mock_emit)] == ["inaccessible"]

    # A retry keeps the note stored with the row: no read, no second event.
    target = {"artifact_id": artifact_id, "heading_anchor": 3}
    await _send(deleg_db, "m-retry", target)
    del mock_emit.full[:]
    await _send(deleg_db, "m-retry", target)

    assert _outcomes(mock_emit) == []


@pytest.mark.asyncio
async def test_a_send_that_admission_refuses_reports_nothing(
    deleg_db, viewed_doc, mock_emit, monkeypatch,
):
    from infra import input_preparation

    _path, artifact_id = viewed_doc

    def refuse(*_args, **_kwargs):
        raise ValueError("different payload")

    monkeypatch.setattr(input_preparation, "admit_input", refuse)
    with pytest.raises(ValueError, match="different payload"):
        await _send(deleg_db, "m-refused", {"artifact_id": artifact_id, "heading_anchor": 3})

    assert _outcomes(mock_emit) == []


@pytest.mark.asyncio
async def test_a_duplicate_that_resolved_a_fresh_note_reports_nothing(
    deleg_db, viewed_doc, mock_emit, monkeypatch,
):
    from infra import input_preparation

    _path, artifact_id = viewed_doc
    # Two sends of one input can both resolve before either is stored; the
    # second is then admitted as a duplicate and keeps the first one's note.
    monkeypatch.setattr(input_preparation, "admit_input", lambda *_args, **_kwargs: SimpleNamespace(
        accepted=SimpleNamespace(duplicate=True),
    ))

    await input_preparation.admit_prepared_input(
        "sender-self", role="user", message_id="m-raced",
        metadata={KEY: {"artifact_id": artifact_id, "heading_anchor": 3}}, db=deleg_db,
    )
    await _reports_written()

    assert _outcomes(mock_emit) == []


@pytest.mark.asyncio
async def test_the_send_returns_without_waiting_for_its_report(deleg_db, viewed_doc, monkeypatch):
    from infra import input_preparation

    _path, artifact_id = viewed_doc
    _admit(deleg_db, "busy")
    release, reported = threading.Event(), []

    def slow_report(thread_id, outcome):
        release.wait(timeout=10)
        reported.append((thread_id, outcome))

    from infra.send_service import send_thread_input

    monkeypatch.setattr(input_preparation, "_report", slow_report)
    thread = dict(deleg_db.execute("SELECT * FROM threads WHERE id='sender-self'").fetchone())
    try:
        await send_thread_input(
            "sender-self", thread, "Review this section", native_mode="ws", ws=AsyncMock(),
            delivery_intent="queue_after_turn", input_id="m-slow",
            metadata={KEY: {"artifact_id": artifact_id, "heading_anchor": 3}},
        )
        # The send is back, its row stored, while the report is still being written.
        assert deleg_db.execute("SELECT 1 FROM messages WHERE id='m-slow'").fetchone()
        assert reported == [] and len(input_preparation._pending_reports) == 1
    finally:
        release.set()
    await _reports_written()

    assert reported == [("sender-self", "path_only")]
    assert not input_preparation._pending_reports


def test_the_editor_sends_no_longer_a_heading_line_than_the_server_compares():
    from infra.document_viewing import _SHOWN_LINE_CAP

    editor = Path(__file__).resolve().parents[2] / "frontend/src/lib/cm/viewing-heading.ts"
    sent = re.search(r"const HEADING_LINE_SENT = (\d+);", editor.read_text())

    assert sent and int(sent.group(1)) == _SHOWN_LINE_CAP


def test_a_document_that_no_longer_exists_resolves_as_unresolved(deleg_db, viewed_doc):
    from infra import input_preparation
    from infra.document_viewing import materialize_viewing_document

    path, artifact_id = viewed_doc
    source = input_preparation.prepare_viewing_document(
        "sender-self", "user", "m-gone", {KEY: {"artifact_id": artifact_id}}, deleg_db,
    )
    path.unlink()
    prepared = materialize_viewing_document(source)

    assert (prepared.note, prepared.outcome) == (None, "unresolved")
