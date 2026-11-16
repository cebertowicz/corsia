"""A whole-file rewrite that deletes a commented line leaves its comment 'text gone' where the line was.

The live document follows a comment through the diff of the rewrite. The diff must not keep a
character of the deleted line as a character of a neighbouring line, or the comment lands there.
"""
import pytest

from doceditor.comment_anchoring import resolve_threads
from doceditor.live_document import LiveDocument
from doceditor.live_text import apply_text, clone, create, follow_spans, keep_standing

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
    text.observe(lambda event: moved.append(keep_standing(
        [span], follow_spans([span], before, event.delta, set())[0], before, str(text))[0]))
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
    rows = 100  # 10,000 line pairs, but short lines: only the line-pair bound can replace it whole
    old = "".join(f"{number % 10}\n" for number in range(rows))
    new = "".join(f"{(number * 7 + 3) % 10}x\n" for number in range(rows))
    assert rows * rows > live_diff.MAX_LINE_PAIRS
    assert len(old) < live_diff.MAX_CHARACTER_BLOCK and len(new) < live_diff.MAX_CHARACTER_BLOCK
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


def test_a_few_long_lines_are_replaced_whole_by_the_work_bound_without_pairing_them(monkeypatch):
    """Three lines a side is nine line pairs and under the character cap: only the work bound replaces it."""
    from doceditor import live_diff

    calls = []
    real = live_diff._alike
    monkeypatch.setattr(live_diff, "_alike", lambda left, right: calls.append(1) or real(left, right))
    old = "".join(f"{'ab ' * 130}{number}\n" for number in range(3))
    new = "".join(f"{'ba ' * 130}{number}\n" for number in range(3))
    assert len(old.splitlines()) ** 2 <= live_diff.MAX_LINE_PAIRS
    assert len(old) < live_diff.MAX_CHARACTER_BLOCK and len(new) < live_diff.MAX_CHARACTER_BLOCK
    assert live_diff._pair_work(old.splitlines(True), new.splitlines(True)) > live_diff.MAX_PAIR_WORK
    assert live_diff.text_changes(old, new) == [(0, len(old.encode()), new)]
    assert calls == []


PARAGRAPH = " ".join(f"word{number} common" if number % 7 == 0 else f"word{number}" for number in range(90))


def test_a_one_word_edit_in_a_long_paragraph_keeps_a_comment_on_its_words():
    """One line replaced by one line is never replaced whole by the pairing bounds (dev kept these comments)."""
    old = HEAD + PARAGRAPH + "\n" + TAIL
    new = HEAD + PARAGRAPH.replace("common", "usual", 1) + "\n" + TAIL
    assert len(PARAGRAPH) > 400
    comment = _after_rewrite(old, new, "word40 word41 word42")
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "word40 word41 word42"


def test_a_long_line_split_in_two_keeps_a_comment_on_the_words_that_moved_to_the_second_line():
    words = PARAGRAPH.split()
    half = len(words) // 2
    first, second = " ".join(words[:half]), " ".join(words[half:])
    old = HEAD + f"{first} {second}\n" + TAIL
    new = HEAD + f"{first}\n{second}\n" + TAIL
    quote = " ".join(words[half + 3:half + 8])
    assert old.count(quote) == 1 and 180 < len(first) and len(old) > 360
    comment = _after_rewrite(old, new, quote)
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == quote


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


SAME_SPAN_NEW = HEAD + "- zzzzzz queue qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n- different words entirely\n" + TAIL


def _kept_on_its_words(comment: dict, new: str) -> None:
    assert comment["orphaned"] is False
    assert new[comment["from_off"]:comment["to_off"]] == "queue"


def test_the_surviving_words_at_the_same_offset_stay_anchored_live():
    _kept_on_its_words(_after_rewrite(PARTIAL_OLD, SAME_SPAN_NEW, "queue"), SAME_SPAN_NEW)


def test_the_surviving_words_at_the_same_offset_stay_anchored_at_rest():
    """A document read from disk keeps a quote still at its stored span, the same answer as live."""
    comment = _comment(PARTIAL_OLD, "queue")
    resolve_threads([comment], SAME_SPAN_NEW)
    _kept_on_its_words(comment, SAME_SPAN_NEW)


@pytest.mark.asyncio
@pytest.mark.parametrize("new, kept", [
    (SAME_SPAN_NEW, True),
    (HEAD + "the queue drains nightly\n- different words entirely\n" + TAIL, False),
])
async def test_a_real_live_document_places_the_comment_as_a_reload_would(tmp_path, new, kept):
    path = tmp_path / "doc.md"
    path.write_text(PARTIAL_OLD)
    live = await LiveDocument.open(path, tmp_path / "state")
    try:
        comment = _comment(PARTIAL_OLD, "queue")
        live.track_range("c", comment["from_off"], comment["to_off"])
        apply_text(live.doc, new)
        assert live.text == new
        start, end = live.ranges["c"]
        assert (new[start:end] == "queue") is kept and (start == end) is not kept
        reloaded = _comment(PARTIAL_OLD, "queue")
        resolve_threads([reloaded], new)
        assert reloaded["orphaned"] is not kept
    finally:
        live.close()


def test_the_candidate_that_reaches_disk_keeps_the_same_span_too():
    from types import SimpleNamespace

    from doceditor.live_comments import candidate_ranges

    comment = _comment(PARTIAL_OLD, "queue")
    span = (comment["from_off"], comment["to_off"])
    live = SimpleNamespace(ranges={"c": span}, open_ends=set(), doc=create(PARTIAL_OLD))
    fork = clone(live.doc.get_update())
    apply_text(fork, SAME_SPAN_NEW)
    assert candidate_ranges(live, fork) == {"c": span}


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
