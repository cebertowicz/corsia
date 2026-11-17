"""A reload agrees with the live document: a comment on a deleted line stays text-gone and is never moved.

While a document is open, a rewrite that deletes a commented line leaves its comment text-gone where the line was.
The next load re-anchors from the stored offsets and context alone; it must reach the same answer, and the same
holds when the file changed on disk with no live session open.
"""
import pytest

from doceditor.comment_anchoring import resolve_threads
from doceditor.quote_anchor import anchor_context

HEAD = "# Notes\n\nSome intro text here.\n\n"
TAIL = "\nThe end.\n"
OLD = HEAD + "- deploy queue merge\n- deploy docs test\n" + TAIL
NEW = HEAD + "- deploy docs test\n" + TAIL


def _stored(old: str, quote: str, *, collapsed_at: int | None = None) -> dict:
    """A comment row as stored: context of the old text, offsets at its span or collapsed where the live path left it."""
    start = old.index(quote)
    end = start + len(quote)
    prefix, suffix = anchor_context(old, start, end, quote)
    from_off, to_off = (start, end) if collapsed_at is None else (collapsed_at, collapsed_at)
    return {"id": "c", "parent_id": None, "review": None, "anchor_text": quote, "from_off": from_off, "to_off": to_off,
            "anchor_prefix": prefix, "anchor_suffix": suffix, "decision": None, "replacement": None}


@pytest.mark.parametrize("quote", ["deploy", "deploy queue merge"])
@pytest.mark.parametrize("collapsed", [False, True])
def test_a_reload_leaves_a_comment_on_a_deleted_line_text_gone(quote, collapsed):
    comment = _stored(OLD, quote, collapsed_at=len(HEAD) if collapsed else None)
    stored_place = (comment["from_off"], comment["to_off"])
    updates = resolve_threads([comment], NEW)
    assert comment["orphaned"] is True
    assert (comment["from_off"], comment["to_off"]) == stored_place
    assert updates == []
