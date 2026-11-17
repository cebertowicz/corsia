"""A reload agrees with the live document: a comment on a deleted line stays text-gone and is never moved.

While a document is open, a rewrite that deletes a commented line leaves its comment text-gone where the line was.
The next load re-anchors from the stored offsets and context alone; it must reach the same answer, and the same
holds when the file changed on disk with no live session open. A comment whose own line is still there keeps its
place when a neighbouring line goes.
"""
import pytest

from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import STALE_MESSAGE, suggestion_span
from doceditor.comment_anchoring import resolve_threads
from doceditor.quote_anchor import anchor_context

HEAD = "# Notes\n\nSome intro text here.\n\n"
TAIL = "\nThe end.\n"
OLD = HEAD + "- deploy queue merge\n- deploy docs test\n" + TAIL
NEW = HEAD + "- deploy docs test\n" + TAIL


def _stored(old: str, quote: str, *, collapsed_at: int | None = None, nth: int = 0) -> dict:
    """A comment row as stored: context of the old text, offsets at its span or collapsed where the live path left it."""
    start = -1
    for _ in range(nth + 1):
        start = old.index(quote, start + 1)
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


def test_the_line_above_does_not_take_the_comment_either():
    old = HEAD + "- a deploy docs\n- b deploy docs\n" + TAIL
    new = HEAD + "- a deploy docs\n" + TAIL
    comment = _stored(old, "deploy", nth=1)
    stored_place = (comment["from_off"], comment["to_off"])
    resolve_threads([comment], new)
    assert comment["orphaned"] is True
    assert (comment["from_off"], comment["to_off"]) == stored_place


def test_a_blank_line_between_the_items_does_not_hide_the_next_one():
    # Paragraphs, headings and loose lists: the neighbour sits behind a blank line.
    old = HEAD + "- deploy queue merge\n\n- deploy docs test\n" + TAIL
    new = HEAD + "- deploy docs test\n" + TAIL
    comment = _stored(old, "deploy")
    stored_place = (comment["from_off"], comment["to_off"])
    resolve_threads([comment], new)
    assert comment["orphaned"] is True
    assert (comment["from_off"], comment["to_off"]) == stored_place


def test_a_blank_line_between_the_items_does_not_hide_the_one_above():
    old = HEAD + "- a deploy docs\n\n- b deploy docs\n" + TAIL
    new = HEAD + "- a deploy docs\n" + TAIL
    comment = _stored(old, "deploy", nth=1)
    stored_place = (comment["from_off"], comment["to_off"])
    resolve_threads([comment], new)
    assert comment["orphaned"] is True
    assert (comment["from_off"], comment["to_off"]) == stored_place


@pytest.mark.parametrize("gap", ["  \t\n", "\r\n"], ids=["spaces-and-tab", "crlf"])
def test_a_whitespace_only_or_crlf_blank_line_does_not_hide_the_neighbour(gap):
    eol = "\r\n" if gap == "\r\n" else "\n"
    old = HEAD + f"- deploy queue merge{eol}{gap}- deploy docs test{eol}" + TAIL
    new = HEAD + f"- deploy docs test{eol}" + TAIL
    below = _stored(old, "deploy")
    stored_place = (below["from_off"], below["to_off"])
    resolve_threads([below], new)
    assert below["orphaned"] is True
    assert (below["from_off"], below["to_off"]) == stored_place

    old = HEAD + f"- a deploy docs{eol}{gap}- b deploy docs{eol}" + TAIL
    new = HEAD + f"- a deploy docs{eol}" + TAIL
    above = _stored(old, "deploy", nth=1)
    stored_place = (above["from_off"], above["to_off"])
    resolve_threads([above], new)
    assert above["orphaned"] is True
    assert (above["from_off"], above["to_off"]) == stored_place


def test_a_comment_whose_line_stays_keeps_it_across_a_blank_line_when_the_neighbour_goes():
    old = HEAD + "- deploy queue merge\n\n- deploy docs test\n" + TAIL
    new = HEAD + "- deploy queue merge\n" + TAIL
    comment = _stored(old, "deploy")
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert comment["from_off"] == len(HEAD) + 2


def test_a_comment_whose_line_stays_keeps_it_when_the_next_line_goes():
    # The commented line starts like the line that went; its own text around the quote is unchanged.
    new = HEAD + "- deploy queue merge\n" + TAIL
    comment = _stored(OLD, "deploy")
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert (comment["from_off"], comment["to_off"]) == (len(HEAD) + 2, len(HEAD) + 2 + len("deploy"))


def test_a_comment_whose_line_stays_keeps_it_when_the_line_above_goes():
    old = HEAD + "- deploy queue merge\n- deploy docs test\n" + TAIL
    new = HEAD + "- deploy docs test\n" + TAIL
    comment = _stored(old, "deploy", nth=1)
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "deploy"
    assert comment["from_off"] == new.index("deploy")


def test_inside_a_long_line_the_one_sided_rule_still_applies():
    # No line break inside the stored context: nothing tells the lines apart. Text inserted at the start
    # of the line moves the words and one inserted after them changes the stored suffix, so only the
    # prefix matches; the comment must stay on its words as before.
    line = " ".join(f"before{i}" for i in range(12)) + " deploy queue merge " + " ".join(f"after{i}" for i in range(12)) + "\n"
    old = HEAD + line + TAIL
    new = HEAD + ("NEW " + line).replace("queue merge", "queue NEW merge") + TAIL
    comment = _stored(old, "deploy")
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "deploy"
    assert comment["from_off"] == new.index("deploy")


def test_accepting_a_suggestion_on_a_deleted_line_is_refused_as_stale():
    comment = {**_stored(OLD, "deploy"), "replacement": "ship"}
    with pytest.raises(CommentActionError) as refused:
        suggestion_span(comment, NEW)
    assert refused.value.kind == "stale"
    assert refused.value.message == STALE_MESSAGE


def test_a_short_start_of_the_next_line_is_not_evidence_that_the_commented_line_is_the_next_one():
    # The stored context window is filled by the commented line, so only "- " of the next line was stored. The
    # commented line itself starts with "- " too: a marker is not proof of whose line this is.
    line = "- deploy queue merge and rebuild all.\n"
    old = HEAD + line + "- docs\n" + TAIL
    new = HEAD + "- deploy queue merge and rebuild everything.\n" + TAIL
    comment = _stored(old, "deploy")
    assert comment["anchor_suffix"].endswith("\n- ")
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "deploy"


def test_a_short_end_of_the_line_above_is_not_evidence_that_the_commented_line_is_the_one_above():
    # The line above and the start of the commented line were edited, so only its end still matches; that
    # line also ends with a full stop, like the stored "." of the line above.
    lead = "- " + "w" * 27 + " "
    old = HEAD + "Alpha line one.\n" + lead + "deploy tail.\n" + TAIL
    new = HEAD + "Alpha line two\n" + "- " + "x" * 27 + " " + "deploy tail.\n" + TAIL
    comment = _stored(old, "deploy")
    assert comment["anchor_prefix"].split("\n")[0] == "."
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "deploy"
