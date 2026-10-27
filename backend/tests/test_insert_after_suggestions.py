"""Insertion suggestions preserve their quoted anchor and each independent fact."""

import sqlite3

import pytest

from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import applied_text, suggestion_span
from doceditor.comment_batch_actions import _batch_text
from doceditor.comment_validation import validate_create
from infra.migration_148_to_149_doc_insert_after import migrate_148_to_149


def row(identifier, text, created):
    return {"id": identifier, "anchor_text": "Heading", "from_off": 0, "to_off": 7,
            "anchor_prefix": "", "anchor_suffix": "\n", "replacement": text,
            "insert_after": True, "created_at": created, "author_thread_id": "agent"}


def test_insert_validation_and_staleness():
    args = dict(body=None, parent_id=None, review=False, quote="Heading", replacement="Heading",
                doc_path="doc.md", artifact_id=None, insert_after=True)
    assert validate_create(**args) == ""
    with pytest.raises(CommentActionError, match="stale|no longer"):
        suggestion_span(row("one", " fact", "1"), "Changed\n")
    with pytest.raises(CommentActionError):
        validate_create(**{**args, "replacement": None})


def test_insert_batch_preserves_order_and_heading():
    rows = [row("a", " one", "1"), row("b", " two", "2")]
    text, accepted, skipped = _batch_text(rows, "Heading\n")
    assert text == "Heading one two\n"
    assert accepted == ["a", "b"]
    assert skipped == []


def test_single_insert_skips_accepted_older_sibling():
    first = row("a", " one", "1")
    second = row("b", " two", "2")
    second["_accepted_insertions"] = [first]
    assert applied_text(first, "Heading\n") == "Heading one\n"
    assert applied_text(second, "Heading one\n") == "Heading one two\n"
    assert applied_text(first, "Heading two\n") == "Heading one two\n"


def test_overlap_allows_shared_point_but_refuses_insertion_inside_replacement(monkeypatch):
    from doceditor import suggestion_overlap

    existing = row("a", " one", "1")
    existing.update(parent_id=None, decision=None, resolved=False, author="agent")
    monkeypatch.setattr(suggestion_overlap, "anchored_threads", lambda *args, **kwargs: ([existing], True))
    suggestion_overlap.check_agent_suggestion_fits(None, "doc", "file", 7, 7)
    existing.update(insert_after=False, from_off=0, to_off=10)
    with pytest.raises(CommentActionError, match="overlaps"):
        suggestion_overlap.check_agent_suggestion_fits(None, "doc", "file", 7, 7)


def test_streamed_preview_moves_from_quote_to_insert_point():
    from types import SimpleNamespace
    from doceditor.live_preview_args import ArgScanner
    from doceditor.live_preview_comments import project_comment

    scanner = ArgScanner()
    scanner.feed('{"quote":"Heading","replacement":" one",')
    slots = {}
    _, first = project_comment(scanner, SimpleNamespace(ranges={}), "Heading\n", slots)
    assert (first[0]["from"], first[0]["to"]) == (0, 7)
    scanner.feed('"insert_after":true}')
    _, second = project_comment(scanner, SimpleNamespace(ranges={}), "Heading\n", slots)
    assert (second[0]["from"], second[0]["to"]) == (7, 7)


def test_lost_anchor_is_not_mistaken_for_an_applied_insertion():
    from doceditor.feedback_suggestion_decisions import _named
    from doceditor.suggestion_stranding import _applied

    suggestion = row("one", " one", "1")
    assert not _applied(suggestion, " one\n")
    assert _named(suggestion) == "“one” (one)"


def test_migration_adds_default_for_existing_rows():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO doc_comments VALUES ('old')")
    migrate_148_to_149(db)
    migrate_148_to_149(db)
    assert db.execute("SELECT insert_after FROM doc_comments").fetchone() == (0,)
