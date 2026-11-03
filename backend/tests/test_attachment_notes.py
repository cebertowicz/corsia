"""Attachment notes tolerate what the wire actually carries.

``attachment_notes`` runs while an input is being rendered, before any provider
call. A malformed attachment field therefore aborts the whole Turn -- the user
sees silence rather than a decline -- so note building must never depend on a
field's type being what the frontend intends to send.
"""

from __future__ import annotations

import os
import sys

import pytest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.attachment_notes import attachment_notes, with_attachment_notes  # noqa: E402
from shared_send_fixtures import _insert_session  # noqa: E402


def _register(tmp_path, monkeypatch, thread_id, file_id, name, *, scope=None):
    from infra import config
    from registry import models

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    scope = scope or tmp_path
    path = scope / ".threads" / thread_id / "uploads" / file_id / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    models.register_artifact(
        integration="upload", type="upload", external_id=file_id,
        parent_thread_id=thread_id, path=str(path),
    )
    return path


def _project(root, project_id="p1", name="demo"):
    from infra.database import get_db

    folder = root / "projects" / name
    folder.mkdir(parents=True)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, 't', 't')",
            (project_id, name),
        )
        db.commit()
    finally:
        db.close()
    return folder


_PDF = {"file_id": "f1", "filename": "spec.pdf", "mime_type": "application/pdf"}


def test_recognized_types_still_classify(isolated_db):
    notes = attachment_notes(
        [
            {"mime_type": "image/png", "filename": "photo.png"},
            {"mime_type": "application/pdf", "filename": "spec.pdf"},
            {"mime_type": "text/csv", "filename": "rows.csv"},
            {"mime_type": "application/octet-stream", "filename": "blob.bin"},
        ],
        "t1",
    )

    assert notes.splitlines() == [
        "[Image attached: photo.png]",
        "[Document attached: spec.pdf]",
        "[Document attached: rows.csv]",
        "[File attached: blob.bin]",
    ]


def test_a_registered_upload_names_its_registered_path(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    _insert_session("t1")
    first = _register(root, monkeypatch, "t1", "f1", "spec.pdf")
    second = _register(root, monkeypatch, "t1", "f2", "blob.bin")

    notes = attachment_notes(
        [
            _PDF,
            {"file_id": "f2", "filename": "blob.bin",
             "mime_type": "application/octet-stream"},
        ],
        "t1",
    )

    assert notes.splitlines() == [
        f"[Document attached: spec.pdf, saved at {first}]",
        f"[File attached: blob.bin, saved at {second}]",
    ]


def test_a_project_thread_upload_names_its_registered_path(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    project = _project(root)
    _insert_session("t1", project_id="p1")
    path = _register(root, monkeypatch, "t1", "f1", "spec.pdf", scope=project)

    notes = attachment_notes([_PDF], "t1")

    assert notes == f"[Document attached: spec.pdf, saved at {path}]"


def test_a_thread_that_moved_project_still_shows_the_registered_path(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    _project(root)
    _insert_session("t1", project_id="p1")
    path = _register(root, monkeypatch, "t1", "f1", "spec.pdf")  # workspace root, not p1's

    assert attachment_notes([_PDF], "t1") == f"[Document attached: spec.pdf, saved at {path}]"


@pytest.mark.parametrize("file_id", [None, 7, "", "never-registered"])
def test_an_unregistered_upload_keeps_the_note_without_a_path(isolated_db, file_id):
    notes = attachment_notes([{**_PDF, "file_id": file_id}], "t1")

    assert notes == "[Document attached: spec.pdf]"


def test_another_threads_upload_has_no_path(isolated_db, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _insert_session("t1")
    _insert_session("t2")
    _register(root, monkeypatch, "t2", "f2", "other.pdf")

    notes = attachment_notes(
        [{"file_id": "f2", "filename": "other.pdf", "mime_type": "application/pdf"}], "t1",
    )

    assert notes == "[Document attached: other.pdf]"


def test_an_upload_of_a_thread_that_no_longer_exists_still_shows_its_path(
    isolated_db, tmp_path, monkeypatch
):
    path = _register(tmp_path.resolve(), monkeypatch, "t1", "f1", "spec.pdf")

    assert attachment_notes([_PDF], "t1") == f"[Document attached: spec.pdf, saved at {path}]"


def test_an_image_keeps_its_bare_note_even_when_registered(
    isolated_db, tmp_path, monkeypatch
):
    _insert_session("t1")
    _register(tmp_path.resolve(), monkeypatch, "t1", "f1", "photo.png")

    notes = attachment_notes(
        [{"file_id": "f1", "filename": "photo.png", "mime_type": "image/png"}], "t1",
    )

    assert notes == "[Image attached: photo.png]"


def test_a_missing_mime_type_falls_back_to_a_file_note(isolated_db):
    assert attachment_notes([{"filename": "notes.txt"}], "t1") == (
        "[File attached: notes.txt]"
    )


def test_a_missing_filename_keeps_a_generic_note(isolated_db):
    assert attachment_notes([{"mime_type": "image/png"}], "t1") == (
        "[Image attached: file]"
    )


@pytest.mark.parametrize("mime_type", [None, 0, 17, True, ["image/png"], object()])
def test_a_non_string_mime_type_never_aborts_turn_construction(isolated_db, mime_type):
    """Any type the wire can carry produces a note, not an AttributeError.

    ``None`` is the common one: an attachment whose type was never resolved
    arrives with the key present and the value null, so ``dict.get`` hands back
    ``None`` rather than the default and the prefix check used to raise.
    """
    notes = attachment_notes(
        [{"mime_type": mime_type, "filename": "mystery.bin"}], "t1",
    )

    assert notes == "[File attached: mystery.bin]"


@pytest.mark.parametrize("attachments", [None, [], ["not a dict", 3, None]])
def test_nothing_attachable_yields_no_notes(isolated_db, attachments):
    assert attachment_notes(attachments, "t1") == ""


def test_notes_follow_the_input_after_a_blank_line():
    assert with_attachment_notes("did you see this", "[File attached: a]") == (
        "did you see this\n\n[File attached: a]"
    )
    assert with_attachment_notes("", "[File attached: a]") == "[File attached: a]"
    assert with_attachment_notes("plain", "") == "plain"


def test_a_registry_error_fails_the_render_instead_of_dropping_the_path(
    isolated_db, monkeypatch,
):
    """A failed lookup is not a missing upload: it raises like any other render read."""
    import sqlite3

    def broken(file_id, db=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("registry.models.resolve_upload", broken)

    with pytest.raises(sqlite3.OperationalError):
        attachment_notes([_PDF], "t1")


@pytest.mark.asyncio
async def test_a_send_without_a_path_to_look_up_never_touches_the_registry(
    isolated_db, monkeypatch,
):
    """Plain text and image-only sends do no registry lookup and no DB hop."""
    from core.attachment_notes import member_notes_async

    def forbidden(*_args, **_kwargs):
        raise AssertionError("registry consulted")

    monkeypatch.setattr("registry.models.resolve_upload", forbidden)
    monkeypatch.setattr("infra.db_async.run_in_db", forbidden)

    assert await member_notes_async([[], []], "t1") == ["", ""]
    assert await member_notes_async(
        [[{"file_id": "f1", "filename": "p.png", "mime_type": "image/png"}]], "t1",
    ) == ["[Image attached: p.png]"]


@pytest.mark.asyncio
async def test_a_document_attachment_still_looks_its_path_up(
    isolated_db, tmp_path, monkeypatch,
):
    from core.attachment_notes import member_notes_async

    root = tmp_path.resolve()
    project = _project(root)
    _insert_session("t1", project_id="p1")
    path = _register(root, monkeypatch, "t1", "f1", "spec.pdf", scope=project)

    assert await member_notes_async([[_PDF]], "t1") == [
        f"[Document attached: spec.pdf, saved at {path}]"
    ]


@pytest.mark.asyncio
async def test_a_send_without_attachments_projects_with_no_registry_or_db_hop(monkeypatch):
    """A text-only projection renders without consulting the upload registry."""
    from infra.input_render import _load_input_projection_async
    from tests.test_clean_batch_render import _batch

    def forbidden(*_args, **_kwargs):
        raise AssertionError("upload registry consulted")

    monkeypatch.setattr("registry.models.resolve_upload", forbidden)
    monkeypatch.setattr("infra.db_async.run_in_db", forbidden)

    projection = await _load_input_projection_async(
        _batch(), {"user_id": "owner"}, SimpleNamespace(),
    )

    assert "attached" not in projection.content


@pytest.mark.asyncio
async def test_the_live_note_and_the_replayed_reference_name_the_same_file(
    main_db, monkeypatch, tmp_path,
):
    """One lookup owns the path: the live note shows the replay block's `path` string."""
    import io

    from fastapi import UploadFile

    from api import uploads
    from history.adapters import to_native_history
    from history.projection import render_portable_history
    from infra import config
    from infra.queries import insert_message

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _token: {"id": "steve"})
    uploaded = await uploads.upload_file(
        None,
        UploadFile(filename="notes.txt", file=io.BytesIO(b"hello"),
                   headers={"content-type": "text/plain"}),
        thread_id="main-1",
    )
    insert_message(
        "main-1", "user", "Read this file",
        metadata={"attachments": [uploaded], "turn_unbound_reason": "legacy_import"},
        db=main_db,
    )

    live = attachment_notes([uploaded], "main-1", db=main_db)
    replayed = to_native_history(render_portable_history("main-1", db=main_db))[0]["content"]

    shown = live.split("saved at ", 1)[1].rstrip("]")
    assert shown.startswith(str(workspace.resolve()))
    assert shown in replayed



def _project_batch(batch, monkeypatch):
    from infra.input_render import project_claimed_input
    from tests.test_clean_batch_render import _schema_db

    monkeypatch.setattr(
        "core.media_delivery.build_delivery_projection",
        lambda attachments, **_kw: (list(attachments), SimpleNamespace(digests=())),
    )
    monkeypatch.setattr(
        "core.attachment_notes.registered_upload_path",
        lambda file_id, thread_id, db=None: f"/ws/.threads/{thread_id}/uploads/{file_id}/spec.pdf",
    )
    db = _schema_db()
    try:
        return project_claimed_input(db, batch, thread_row={"user_id": "owner"}, target=object())
    finally:
        db.close()


_NOTE = "[Document attached: spec.pdf, saved at /ws/.threads/thread-1/uploads/f1/spec.pdf]"


def test_a_members_note_sits_inside_that_members_own_text(monkeypatch):
    """Only member 1 has a file: its note is before member 1's footer and member 2."""
    from dataclasses import replace

    from core.model_context_sources import agent_send_envelope_source_entry
    from tests.test_clean_batch_render import _batch

    plain = _batch()
    batch = replace(plain, items=(
        replace(plain.items[0], delivery_options={"attachments": [_PDF]}),
        plain.items[1],
    ))

    projected = _project_batch(batch, monkeypatch)

    content = projected.content
    assert content.index("first") < content.index(_NOTE) < content.index("[To reply")
    assert content.index(_NOTE) < content.index("[2/2]")
    assert content.count(_NOTE) == 1
    assert _NOTE in projected.source_content
    assert _NOTE in projected.agent_envelope.body_content
    assert projected.source_content == projected.agent_envelope.body_content

    def framing(envelope):
        return agent_send_envelope_source_entry(
            thread_id="thread-1", harness="native", agent_envelope=envelope,
        )

    # The note is the person's message, not framing: the framing entry is unchanged.
    unnoted = _project_batch(plain, monkeypatch)
    assert framing(projected.agent_envelope).prompt_content == framing(
        unnoted.agent_envelope
    ).prompt_content
    # Nothing that persists or replays the message sees the note.
    assert projected.receipts == batch.items
    assert all(_NOTE not in item.content for item in projected.receipts)


def test_a_single_members_note_follows_its_text_after_a_blank_line(monkeypatch):
    from dataclasses import replace

    from tests.test_clean_batch_render import _batch

    one = _batch()
    batch = replace(one, items=(
        replace(one.items[1], delivery_options={"attachments": [_PDF]}),
    ))

    assert _project_batch(batch, monkeypatch).content == f"second\n\n{_NOTE}"
