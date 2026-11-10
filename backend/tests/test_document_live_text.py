"""Real CRDT text operations preserve item identities and Unicode positions."""
from __future__ import annotations

from pycrdt import Doc, Text


def document(text=""):
    doc = Doc()
    doc["text"] = Text(text)
    return doc


def test_agent_diff_preserves_concurrent_user_text():
    from doceditor.live_text import apply_text, clone

    base = document("hello world")
    browser = clone(base.get_update())
    browser["text"].insert(0, "USER ")
    agent = clone(base.get_update())
    before = agent.get_state()
    apply_text(agent, "hello there")
    browser.apply_update(agent.get_update(before))
    assert str(browser["text"]) == "USER hello there"


def test_unicode_diff_uses_pycrdt_byte_offsets():
    from doceditor.live_text import apply_text

    doc = document("aé🙂z")
    apply_text(doc, "aé🙃Z")
    assert str(doc["text"]) == "aé🙃Z"
    apply_text(doc, "é🙃")
    assert str(doc["text"]) == "é🙃"


def test_utf16_range_maps_remote_unicode_delta():
    from doceditor.live_text import clone, map_offset

    doc = document("é hello 🙂 world")
    before = str(doc["text"])
    deltas = []
    text = doc["text"]
    subscription = text.observe(lambda event: deltas.append(event.delta))
    other = clone(doc.get_update())
    other["text"].insert(0, "🙂 ")
    doc.apply_update(other.get_update(doc.get_state()))
    assert map_offset(2, before, deltas[0], right=False) == 5
    assert map_offset(7, before, deltas[0], right=True) == 10
    text.unobserve(subscription)


def test_deletion_delta_collapses_without_quote_guess():
    from doceditor.live_text import apply_text, map_offset

    doc = document("one quote two")
    deltas = []
    text = doc["text"]
    subscription = text.observe(lambda event: deltas.append(event.delta))
    apply_text(doc, "one  two")
    assert map_offset(4, "one quote two", deltas[0], right=False) == 4
    assert map_offset(9, "one quote two", deltas[0], right=True) == 4
    text.unobserve(subscription)


def test_exclusive_boundaries_and_single_point_association():
    from doceditor.live_text import map_offset

    assert map_offset(1, "abc", [{"retain": 1}, {"insert": "🙂"}], right=False) == 3
    assert map_offset(2, "abc", [{"retain": 2}, {"insert": "🙂"}], right=True) == 2
    assert map_offset(1, "abc", [{"retain": 1}, {"insert": "🙂"}], right=True) == 1


def _edited(text, edit):
    """The delta a browser's edit of ``text`` delivers, made with real CRDT operations."""
    doc = document(text)
    deltas = []
    shared = doc["text"]
    subscription = shared.observe(lambda event: deltas.append(event.delta))
    with doc.transaction():
        edit(shared)
    shared.unobserve(subscription)
    return deltas[0]


def test_typing_over_a_whole_span_moves_it_onto_the_typed_text():
    from doceditor.live_text import map_spans

    def type_over(text):
        del text[4:9]
        text.insert(4, "words!")

    assert map_spans([(4, 9)], "one quote two", _edited("one quote two", type_over)) == [(4, 10)]
    # Either order of the two halves of one edit lands the same way.
    for delta in ([{"retain": 4}, {"insert": "words!"}, {"delete": 5}], [{"retain": 4}, {"delete": 5}, {"insert": "words!"}]):
        assert map_spans([(4, 9)], "one quote two", delta) == [(4, 10)]
        [(first, last)] = map_spans([(4, 9)], "one quote two", delta, follow_typing=False)
        assert first == last


def _typed(text, span, edits):
    """A span followed through ``edits``, each its own transaction, the way a person types a key at a time."""
    from doceditor.live_text import follow_spans

    doc = document(text)
    shared = doc["text"]
    spans, opened = [span], set()
    def changed(event):
        nonlocal spans, opened
        spans, opened = follow_spans(spans, before, event.delta, opened)
    subscription = shared.observe(changed)
    for edit in edits:
        before = str(shared)
        with doc.transaction():
            edit(shared)
    shared.unobserve(subscription)
    return spans[0], bool(opened), str(shared)


def _insert(at, value):
    return lambda text: text.insert(at, value)


def _replace(start, end, value):
    def edit(text):
        del text[start:end]
        text.insert(start, value)
    return edit


# The browser's cases (``cm/comment-range.test.ts``), with the same text and the same results.
TYPE_OVER = [_replace(4, 9, "H"), *(_insert(5 + i, ch) for i, ch in enumerate("andoff"))]


def test_a_word_typed_over_a_span_a_key_at_a_time_stays_on_it():
    span, opened, text = _typed("one quote two", (4, 9), TYPE_OVER)
    assert (span, opened) == ((4, 11), True)
    assert text[4:11] == "Handoff"


def test_the_open_end_closes_at_the_first_edit_elsewhere():
    assert _typed("one quote two", (4, 9), [*TYPE_OVER, _insert(0, "X"), _insert(12, "!")])[:2] == ((5, 12), False)
    assert _typed("one quote two", (4, 9), [_replace(4, 9, "H"), _insert(4, ">"), _insert(6, "a")])[:2] == ((5, 6), False)
    # An untouched span's end stays exclusive, and a paste over the span still moves it onto the paste.
    assert _typed("one quote two", (4, 9), [_insert(9, "A"), _insert(10, "B")])[:2] == ((4, 9), False)
    assert _typed("one quote two", (4, 9), [_replace(4, 9, "words!")])[:2] == ((4, 10), True)


def test_the_open_end_counts_utf16_like_the_browser():
    span, _, text = _typed("one quote two", (4, 9), [_replace(4, 9, "é"), _insert(5, "🙂")])
    assert span == (4, 7)
    assert text.encode("utf-16-le")[8:14].decode("utf-16-le") == "é🙂"


def test_deleting_a_whole_span_collapses_it_and_edges_stay_exclusive():
    from doceditor.live_text import map_offset, map_spans

    def delete(text):
        del text[4:9]

    assert map_spans([(4, 9)], "one quote two", _edited("one quote two", delete)) == [(4, 4)]
    # Typed at the first or the last character: outside the span, which keeps its text.
    assert map_spans([(4, 9)], "one quote two", [{"retain": 4}, {"insert": "AB"}]) == [(6, 11)]
    assert map_spans([(4, 9)], "one quote two", [{"retain": 9}, {"insert": "AB"}]) == [(4, 9)]
    # A collapsed span stays before what is typed at it, and an edit that leaves part of a span's text keeps that part.
    assert map_spans([(4, 4)], "one  two", [{"retain": 4}, {"insert": "AB"}]) == [(4, 4)]
    partial = [{"retain": 2}, {"delete": 4}, {"insert": "X"}]
    assert map_spans([(4, 9)], "one quote two", partial) == [
        (map_offset(4, "one quote two", partial, right=False), map_offset(9, "one quote two", partial, right=True))]


def test_utf16_boundary_conversion_and_digest_format():
    from doceditor.live_text import byte_offset, text_digest
    import hashlib
    import pytest

    assert byte_offset("é🙂z", 1) == 2
    assert byte_offset("é🙂z", 3) == 6
    with pytest.raises(ValueError):
        byte_offset("é🙂z", 2)
    assert text_digest("é🙂z") == hashlib.sha256("é🙂z".encode()).hexdigest()[:16]


def test_multi_operation_delta_maps_skin_tone_and_combining_marks():
    from doceditor.live_text import map_offset

    before = "é👍🏽a\u0301QUOTE中"
    delta = [{"retain": 2}, {"delete": 8}, {"insert": "🙂 "}, {"retain": 3}, {"insert": "X"}]
    assert map_offset(7, before, delta, right=False) == 7
    assert map_offset(12, before, delta, right=True) == 12
