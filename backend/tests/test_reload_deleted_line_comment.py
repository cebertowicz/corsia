"""A reload agrees with the live document: a comment on a deleted line stays text-gone and is never moved.

While a document is open, a rewrite that deletes a commented line leaves its comment text-gone where the line was
(its stored span collapses to a point). The next load re-anchors from the stored offsets and context alone; it must
reach the same answer. A comment whose own line is still there keeps its place when a neighbouring line goes.
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


def _assert_text_gone_in_place(comment: dict, stored_place: tuple[int, int]) -> None:
    assert comment["orphaned"] is True
    assert (comment["from_off"], comment["to_off"]) == stored_place


@pytest.mark.parametrize(("quote", "collapsed"), [("deploy", True), ("deploy queue merge", True), ("deploy queue merge", False)])
def test_a_reload_leaves_a_comment_on_a_deleted_line_text_gone(quote, collapsed):
    comment = _stored(OLD, quote, collapsed_at=len(HEAD) if collapsed else None)
    stored_place = (comment["from_off"], comment["to_off"])
    updates = resolve_threads([comment], NEW)
    _assert_text_gone_in_place(comment, stored_place)
    assert updates == []


def test_the_line_above_does_not_take_the_comment_either():
    old = HEAD + "- a deploy docs\n- b deploy docs\n" + TAIL
    new = HEAD + "- a deploy docs\n" + TAIL
    comment = _stored(old, "deploy", nth=1, collapsed_at=len(HEAD) + len("- a deploy docs\n"))
    stored_place = (comment["from_off"], comment["to_off"])
    resolve_threads([comment], new)
    _assert_text_gone_in_place(comment, stored_place)


@pytest.mark.parametrize(("eol", "gap"), [("\n", "\n"), ("\n", "  \t\n"), ("\r\n", "\r\n")],
                         ids=["blank", "spaces-and-tab", "crlf"])
def test_a_blank_line_between_the_items_changes_nothing(eol, gap):
    old = HEAD + f"- deploy queue merge{eol}{gap}- deploy docs test{eol}" + TAIL
    new = HEAD + f"- deploy docs test{eol}" + TAIL
    below = _stored(old, "deploy", collapsed_at=len(HEAD))
    stored_place = (below["from_off"], below["to_off"])
    resolve_threads([below], new)
    _assert_text_gone_in_place(below, stored_place)

    old = HEAD + f"- a deploy docs{eol}{gap}- b deploy docs{eol}" + TAIL
    new = HEAD + f"- a deploy docs{eol}" + TAIL
    above = _stored(old, "deploy", nth=1, collapsed_at=len(HEAD) + len(f"- a deploy docs{eol}"))
    stored_place = (above["from_off"], above["to_off"])
    resolve_threads([above], new)
    _assert_text_gone_in_place(above, stored_place)


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


def test_a_collapsed_comment_comes_back_where_its_text_is_restored():
    # Deleted text restored in the same place: every stored side matches again, so the comment is placed on it.
    comment = _stored(OLD, "deploy", collapsed_at=len(HEAD))
    resolve_threads([comment], OLD)
    assert comment["orphaned"] is False
    assert (comment["from_off"], comment["to_off"]) == (len(HEAD) + 2, len(HEAD) + 2 + len("deploy"))


def test_inside_a_long_line_the_one_sided_rule_still_applies():
    # Text inserted at the start of the line moves the words and one inserted after them changes the stored
    # suffix, so only the prefix matches; a comment that is not collapsed stays on its words as before.
    line = " ".join(f"before{i}" for i in range(12)) + " deploy queue merge " + " ".join(f"after{i}" for i in range(12)) + "\n"
    old = HEAD + line + TAIL
    new = HEAD + ("NEW " + line).replace("queue merge", "queue NEW merge") + TAIL
    comment = _stored(old, "deploy")
    resolve_threads([comment], new)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "deploy"
    assert comment["from_off"] == new.index("deploy")


def test_accepting_a_suggestion_on_a_deleted_line_is_refused_as_stale():
    comment = {**_stored(OLD, "deploy", collapsed_at=len(HEAD)), "replacement": "ship"}
    with pytest.raises(CommentActionError) as refused:
        suggestion_span(comment, NEW)
    assert refused.value.kind == "stale"
    assert refused.value.message == STALE_MESSAGE
