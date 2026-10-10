"""Attachment notes tolerate what the wire actually carries.

``build_agent_content`` runs while a Turn is being constructed, before any
provider call. A malformed attachment field therefore aborts the whole Turn --
the user sees silence rather than a decline -- so note building must never
depend on a field's type being what the frontend intends to send.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ws.turn_helpers import build_agent_content  # noqa: E402


def test_recognized_types_still_classify():
    content, image_paths = build_agent_content(
        "look at these",
        [
            {"mime_type": "image/png", "filename": "photo.png"},
            {"mime_type": "application/pdf", "filename": "spec.pdf"},
            {"mime_type": "text/csv", "filename": "rows.csv"},
            {"mime_type": "application/octet-stream", "filename": "blob.bin"},
        ],
    )

    assert "[Image attached: photo.png]" in content
    assert "[Document attached: spec.pdf]" in content
    assert "[Document attached: rows.csv]" in content
    assert "[File attached: blob.bin]" in content
    assert image_paths == []


def test_a_missing_mime_type_falls_back_to_a_file_note():
    content, _paths = build_agent_content("", [{"filename": "notes.txt"}])

    assert content == "[File attached: notes.txt]"


def test_a_missing_filename_keeps_a_generic_note():
    content, _paths = build_agent_content("", [{"mime_type": "image/png"}])

    assert content == "[Image attached: file]"


@pytest.mark.parametrize("mime_type", [None, 0, 17, True, ["image/png"], object()])
def test_a_non_string_mime_type_never_aborts_turn_construction(mime_type):
    """Any type the wire can carry produces a note, not an AttributeError.

    ``None`` is the common one: an attachment whose type was never resolved
    arrives with the key present and the value null, so ``dict.get`` hands back
    ``None`` rather than the default and the prefix check used to raise.
    """
    content, _paths = build_agent_content(
        "did you see this", [{"mime_type": mime_type, "filename": "mystery.bin"}]
    )

    assert content == "did you see this\n\n[File attached: mystery.bin]"
