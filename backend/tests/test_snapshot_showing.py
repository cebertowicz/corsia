"""The on-disk showing rule protects the selected line, including ambiguous edits."""

from doceditor.snapshot_showing import evaluate


def test_text_index_matches_utf16_boundaries_and_line_counts():
    from doceditor.quote_anchor import _cp_at_u16
    from doceditor.snapshot_showing import TextIndex

    text = 'alpha\n😀 z\nlast'
    index = TextIndex(text)
    for offset in [None, True, -1, *range(18), 100]:
        assert index.cp_at_u16(offset) == _cp_at_u16(text, offset)
    for position in range(len(text) + 1):
        assert index.line_at(position) == text.count('\n', 0, position)


def test_recognizable_edited_line_keeps_comment():
    old = "one\nold item\nend\n"
    new = "one\nnew item\nend\n"
    assert evaluate(old, new, (4, 12), (4, 12), "old item")[0]


def test_deleted_line_does_not_attach_to_neighbour():
    old = "one\ngone\nend\n"
    new = "one\nend\n"
    assert not evaluate(old, new, (4, 8), (4, 7), "gone")[0]


def test_cross_line_mapping_is_not_accepted():
    old = "one\n5. target\nend\n"
    new = "one\n6. unrelated\n7. target\nend\n"
    start = new.index("6. unrelated")
    end = new.index("\nend")
    assert not evaluate(old, new, (4, 13), (start, end), "5. target")[0]


def test_unchanged_survivor_keeps_its_line():
    old = "top\nuseful words here\nend\n"
    new = "new\ntop\nuseful words here\nend\n"
    start = old.index("words")
    mapped = new.index("words")
    assert evaluate(old, new, (start, start + 5), (mapped, mapped + 5), "words")[0]


def test_collapsed_mapping_is_gone():
    assert not evaluate("abc\n", "\n", (0, 3), (0, 0), "abc")[0]
