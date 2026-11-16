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


def _list(prefix: str, count: int, tag: str) -> str:
    return "".join(f"- {prefix} {number} {tag}\n" for number in range(count))


def test_a_large_rewritten_block_is_replaced_whole_without_pairing_its_lines(monkeypatch):
    """Pairing is one comparison per line pair, so a block past the bound must not be paired at all."""
    from doceditor import live_diff

    calls = []
    real = live_diff._alike
    monkeypatch.setattr(live_diff, "_alike", lambda left, right: calls.append(1) or real(left, right))
    rows = 2000
    old = "".join(f"{number % 10}\n" for number in range(rows))
    new = "".join(f"{(number * 7 + 3) % 10}x\n" for number in range(rows))
    assert rows * rows > live_diff.MAX_LINE_PAIRS and len(old) < live_diff.MAX_CHARACTER_BLOCK
    assert live_diff.text_changes(old, new) == [(0, len(old.encode()), new)]
    assert calls == []


def test_a_block_just_inside_the_bound_is_still_paired(monkeypatch):
    from doceditor import live_diff

    side = int(live_diff.MAX_LINE_PAIRS ** .5)
    old, new = _list("audit", side, "a"), _list("audit", side, "b")
    calls = []
    real = live_diff._alike
    monkeypatch.setattr(live_diff, "_alike", lambda left, right: calls.append(1) or real(left, right))
    changes = live_diff.text_changes(old, new)
    assert calls and len(changes) == side  # one letter per line, not one replaced block


def test_a_comment_in_a_block_past_the_bound_is_text_gone_not_moved():
    side = 30  # 900 line pairs, past the bound
    old = HEAD + _list("audit", side, "a") + TAIL
    new = HEAD + _list("review", side, "b") + TAIL
    comment = _after_rewrite(old, new, "audit 12 a")
    assert comment["orphaned"] is True
    assert comment["from_off"] == comment["to_off"]


PARTIAL_OLD = HEAD + "- deploy queue merge soon\n- other thing here\n" + TAIL


def test_a_few_words_that_survive_in_a_line_rewritten_to_under_half_alike_are_text_gone_live():
    new = HEAD + "the queue drains nightly\n- different words entirely\n" + TAIL
    live = _after_rewrite(PARTIAL_OLD, new, "queue")
    _gone_where_it_was(live, len(HEAD))
    rest = _comment(PARTIAL_OLD, "queue")
    resolve_threads([rest], new)
    assert rest["orphaned"] is True  # reopened from disk the words are not at their old place either


def test_the_surviving_words_at_the_same_offset_are_text_gone_live():
    """At rest, ``reanchor`` keeps a quote that is still at its stored span; see the PR body."""
    new = HEAD + "- zzzzzz queue qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n- different words entirely\n" + TAIL
    _gone_where_it_was(_after_rewrite(PARTIAL_OLD, new, "queue"), len(HEAD))


def test_a_line_split_in_two_keeps_a_comment_on_the_words_that_moved_to_the_second_line():
    old = HEAD + "foo bar baz qux\n" + TAIL
    new = HEAD + "foo bar\nbaz qux\n" + TAIL
    comment = _after_rewrite(old, new, "baz qux")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "baz qux"


def test_three_lines_joined_into_one_keep_a_comment_on_the_middle_line():
    old = HEAD + "a1 x\nb2 y\nc3 z\n" + TAIL
    new = HEAD + "a1 x b2 y c3 z\n" + TAIL
    comment = _after_rewrite(old, new, "b2 y")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "b2 y"


def test_a_paragraph_reflowed_to_other_line_breaks_keeps_a_comment_on_a_word_in_it():
    old = HEAD + "one two three\nfour five six\nseven eight nine\n" + TAIL
    new = HEAD + "one two three four\nfive six seven\neight nine\n" + TAIL
    comment = _after_rewrite(old, new, "five six")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "five six"
