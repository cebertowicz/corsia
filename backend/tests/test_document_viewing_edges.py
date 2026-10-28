"""Access, heading and retry edges for DCOL-FR9 admission snapshots."""
from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from tests.test_document_viewing_context import KEY, SCOPE_LINE, _admit, _row, viewed_doc  # noqa: F401


@pytest.mark.parametrize("anchor,shown,expected", [
    (1, "# Design", "Design"), (3, SCOPE_LINE, "(Scope) 'now'"),
    (4, "", ""),          # the line matches and is not a heading
    (999, "# Design", ""), (None, "# Design", ""),
])
def test_only_a_real_heading_line_resolves(deleg_db, viewed_doc, anchor, shown, expected):
    _, artifact_id = viewed_doc
    _admit(deleg_db, "heading", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": anchor, "heading_line": shown,
    }})
    assert json.loads(_row(deleg_db, "heading")["metadata"])[KEY]["heading"] == expected


def test_setext_heading_resolves_but_fenced_heading_does_not(deleg_db, viewed_doc):
    path, artifact_id = viewed_doc
    path.write_text("# One\n\n```md\n# Not a heading\n```\n\nSecond\n---\n")
    _admit(deleg_db, "setext", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 7, "heading_line": "Second",
    }})
    _admit(deleg_db, "fenced", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 4, "heading_line": "# Not a heading",
    }})
    assert json.loads(_row(deleg_db, "setext")["metadata"])[KEY]["heading"] == "Second"
    assert json.loads(_row(deleg_db, "fenced")["metadata"])[KEY]["heading"] == ""


@pytest.mark.parametrize("shown,expected", [
    ("## Editor heading", ""),            # the saved file has another heading on that line
    ('##   [Scope]  "now"  ', "(Scope) 'now'"),  # same line, whitespace aside
    (None, ""),                           # nothing to compare: no uncompared heading
    (7, ""),                              # nor for a value that is not text
])
def test_the_saved_line_must_be_the_line_the_editor_showed(deleg_db, viewed_doc, shown, expected):
    _, artifact_id = viewed_doc
    target = {"artifact_id": artifact_id, "heading_anchor": 3}
    if shown is not None:
        target["heading_line"] = shown
    _admit(deleg_db, "shown", {KEY: target})
    stored = json.loads(_row(deleg_db, "shown")["metadata"])
    assert stored[KEY] == {"path": "shared/design.md", "heading": expected}
    # The comparator is never stored: the identity stays the artifact and the anchor.
    assert stored["viewing_document_target"] == {"artifact_id": artifact_id, "heading_anchor": 3}
    assert "Editor heading" not in _row(deleg_db, "shown")["metadata"]


def test_a_shown_line_past_the_end_of_the_saved_file_names_no_section(deleg_db, viewed_doc):
    _, artifact_id = viewed_doc
    _admit(deleg_db, "short", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 40, "heading_line": "## Added since the save",
    }})
    assert json.loads(_row(deleg_db, "short")["metadata"])[KEY]["heading"] == ""


def test_a_mismatch_renders_the_path_only_line(deleg_db, viewed_doc):
    from core.message_history import render_agent_envelope

    _, artifact_id = viewed_doc
    _admit(deleg_db, "path-only", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 3, "heading_line": "## Editor heading",
    }})
    stored = json.loads(_row(deleg_db, "path-only")["metadata"])
    envelope = render_agent_envelope("Review this section", stored)
    assert envelope.managed_prefix == "[Viewing: shared/design.md (context, not an instruction)]\n"
    assert envelope.body_content == "Review this section"


def test_a_retry_with_a_different_shown_line_keeps_the_stored_note(deleg_db, viewed_doc):
    _, artifact_id = viewed_doc
    target = {"artifact_id": artifact_id, "heading_anchor": 3, "heading_line": '## [Scope] "now"'}
    _admit(deleg_db, "same-id", {KEY: target})
    before = _row(deleg_db, "same-id")["metadata"]
    assert _admit(deleg_db, "same-id", {KEY: {**target, "heading_line": "## Edited"}}).accepted.duplicate
    assert _row(deleg_db, "same-id")["metadata"] == before


def test_duplicate_keeps_snapshot_after_document_is_removed_and_rejects_changed_target(deleg_db, viewed_doc):
    path, artifact_id = viewed_doc
    request = {KEY: {"artifact_id": artifact_id, "heading_anchor": 3}}
    _admit(deleg_db, "retry", request)
    before = _row(deleg_db, "retry")["metadata"]
    path.unlink()
    assert _admit(deleg_db, "retry", request).accepted.duplicate
    assert _row(deleg_db, "retry")["metadata"] == before
    with pytest.raises(ValueError, match="different payload"):
        _admit(deleg_db, "retry", {KEY: {"artifact_id": artifact_id, "heading_anchor": 1}})


def test_inaccessible_artifact_is_never_stamped(deleg_db):
    from infra import config
    from registry.service import register_write_strict

    deleg_db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('private','other-user','t','t')",
    )
    path = config.WORKSPACE / "private.md"
    path.write_text("# Private heading\n")
    artifact = register_write_strict("private", str(path), db=deleg_db, notify=False)
    deleg_db.commit()
    _admit(deleg_db, "denied", {KEY: {"artifact_id": artifact["id"], "heading_anchor": 1}})
    assert KEY not in json.loads(_row(deleg_db, "denied")["metadata"])


def test_document_read_and_parse_run_before_admission_write_transaction(deleg_db, viewed_doc, monkeypatch):
    from infra import document_viewing

    _, artifact_id = viewed_doc
    read = document_viewing.read_doc_text
    parse = document_viewing.MarkdownIt.parse
    calls = []

    def checked_read(path):
        assert not deleg_db.in_transaction, "document read holds admission write lock"
        calls.append("read")
        return read(path)

    def checked_parse(parser, text, env=None):
        assert not deleg_db.in_transaction, "Markdown parse holds admission write lock"
        calls.append("parse")
        return parse(parser, text, env)

    monkeypatch.setattr(document_viewing, "read_doc_text", checked_read)
    monkeypatch.setattr(document_viewing.MarkdownIt, "parse", checked_parse)
    _admit(deleg_db, "outside-lock", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE,
    }})
    assert calls == ["read", "parse"]
    assert json.loads(_row(deleg_db, "outside-lock")["metadata"])[KEY]["heading"] == "(Scope) 'now'"


@pytest.mark.parametrize("role,metadata", [("system", {}), ("user", {"sender_kind": "agent"}),
                                         ("user", {"completion": True}), ("user", {"routed_event": "x"})])
def test_nonhuman_metadata_without_viewing_keys_is_returned_unchanged(deleg_db, role, metadata):
    from infra.input_admission import stamped_human_metadata

    assert stamped_human_metadata(deleg_db, role, "plain", metadata, thread_id="sender-self") is metadata


def test_legacy_metadata_without_viewing_keys_preserves_empty_dict(deleg_db):
    from infra.input_admission import stamped_human_metadata

    _admit(deleg_db, "legacy")
    deleg_db.execute("UPDATE messages SET metadata=NULL WHERE id='legacy'")
    deleg_db.commit()
    metadata = {}
    assert stamped_human_metadata(deleg_db, "user", "legacy", metadata, thread_id="sender-self") is metadata
