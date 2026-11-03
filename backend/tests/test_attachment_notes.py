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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.attachment_notes import attachment_notes, with_attachment_notes  # noqa: E402


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


def test_a_registered_upload_names_its_path_from_the_workspace_root(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    _register(root, monkeypatch, "t1", "f1", "spec.pdf")
    _register(root, monkeypatch, "t1", "f2", "blob.bin")

    notes = attachment_notes(
        [
            {"file_id": "f1", "filename": "spec.pdf", "mime_type": "application/pdf"},
            {"file_id": "f2", "filename": "blob.bin",
             "mime_type": "application/octet-stream"},
        ],
        "t1",
    )

    assert notes.splitlines() == [
        "[Document attached: spec.pdf, saved at .threads/t1/uploads/f1/spec.pdf]",
        "[File attached: blob.bin, saved at .threads/t1/uploads/f2/blob.bin]",
    ]


def test_a_project_thread_upload_is_relative_to_its_project_root(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    _register(root, monkeypatch, "t1", "f1", "spec.pdf", scope=project)

    notes = attachment_notes(
        [{"file_id": "f1", "filename": "spec.pdf", "mime_type": "application/pdf"}],
        "t1",
    )

    assert notes == (
        "[Document attached: spec.pdf, saved at .threads/t1/uploads/f1/spec.pdf]"
    )


@pytest.mark.parametrize("file_id", [None, 7, "", "never-registered"])
def test_an_unregistered_upload_keeps_the_note_without_a_path(isolated_db, file_id):
    notes = attachment_notes(
        [{"file_id": file_id, "filename": "spec.pdf", "mime_type": "application/pdf"}],
        "t1",
    )

    assert notes == "[Document attached: spec.pdf]"


def test_a_legacy_upload_outside_the_thread_tree_has_no_path(
    isolated_db, tmp_path, monkeypatch
):
    from infra import config
    from registry import models

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    legacy = tmp_path.resolve() / "uploads" / "steve" / "f1" / "spec.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x")
    models.register_artifact(
        integration="upload", type="upload", external_id="f1",
        parent_thread_id="t1", path=str(legacy),
    )

    notes = attachment_notes(
        [{"file_id": "f1", "filename": "spec.pdf", "mime_type": "application/pdf"}],
        "t1",
    )

    assert notes == "[Document attached: spec.pdf]"


def test_a_missing_file_or_another_threads_upload_has_no_path(
    isolated_db, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    gone = _register(root, monkeypatch, "t1", "f1", "spec.pdf")
    gone.unlink()
    _register(root, monkeypatch, "t2", "f2", "other.pdf")

    notes = attachment_notes(
        [
            {"file_id": "f1", "filename": "spec.pdf", "mime_type": "application/pdf"},
            {"file_id": "f2", "filename": "other.pdf", "mime_type": "application/pdf"},
        ],
        "t1",
    )

    assert notes.splitlines() == [
        "[Document attached: spec.pdf]",
        "[Document attached: other.pdf]",
    ]


def test_an_image_keeps_its_bare_note_even_when_registered(
    isolated_db, tmp_path, monkeypatch
):
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
