"""A whole-file rewrite that deletes a commented line leaves its comment 'text gone' where the line was.

The live document follows a comment through the diff of the rewrite. The diff must not keep a
character of the deleted line as a character of a neighbouring line, or the comment lands there.
"""
from doceditor.comment_anchoring import resolve_threads
from doceditor.live_text import apply_text, create, follow_spans

HEAD = "# Plan\n\n- keep one\n"
TAIL = "- keep two\n"


def _comment(old: str, quote: str) -> dict:
    start = old.index(quote)
    end = start + len(quote)
    return {"id": "c", "parent_id": None, "review": False, "anchor_text": quote, "from_off": start, "to_off": end,
            "anchor_prefix": old[max(0, start - 32):start], "anchor_suffix": old[end:end + 32],
            "decision": None, "replacement": None}


def _after_rewrite(old: str, new: str, quote: str) -> dict:
    """The comment's state after a live write replaces ``old`` with ``new``, as the lane and the browser see it."""
    comment = _comment(old, quote)
    span = (comment["from_off"], comment["to_off"])
    doc = create(old)
    text = doc["text"]
    before = str(text)
    moved: list[tuple[int, int]] = []
    text.observe(lambda event: moved.append(follow_spans([span], before, event.delta, set())[0][0]))
    apply_text(doc, new)
    assert str(text) == new
    resolve_threads([comment], new, ranges={"c": moved[-1] if moved else span})
    return comment


def _gone_where_it_was(comment: dict, line_start: int) -> None:
    assert comment["orphaned"] is True
    assert comment["from_off"] == comment["to_off"] == line_start


def test_the_next_line_starting_with_the_same_words_does_not_take_the_comment():
    old = HEAD + "- deploy queue merge\n- deploy docs test\n" + TAIL
    new = HEAD + "- deploy docs test\n" + TAIL
    _gone_where_it_was(_after_rewrite(old, new, "deploy queue merge"), len(HEAD))


def test_a_deleted_line_beside_an_edited_one_does_not_leave_its_letters_in_the_edited_line():
    old = HEAD + "- audit test index cache\n- audit build docs deploy\n" + TAIL
    new = HEAD + "- audit review docs deploy\n" + TAIL
    _gone_where_it_was(_after_rewrite(old, new, "audit test index cache"), len(HEAD))


def test_a_comment_on_the_edited_line_stays_on_it_when_the_line_before_is_deleted():
    old = HEAD + "- audit test index cache\n- audit build docs deploy\n" + TAIL
    new = HEAD + "- audit build docs deploy now\n" + TAIL
    comment = _after_rewrite(old, new, "audit build docs deploy")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "audit build docs deploy"


def test_an_edit_inside_the_commented_line_keeps_the_comment_on_its_words():
    old = HEAD + "- first part of a sentence here\n" + TAIL
    new = HEAD + "- first part of a longer sentence here\n" + TAIL
    comment = _after_rewrite(old, new, "sentence here")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "sentence here"
