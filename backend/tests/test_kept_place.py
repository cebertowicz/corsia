"""A comment sent again from a kept draft goes only where its quote still has both kept sides."""

from doceditor.quote_anchor import _u16, anchor_context, kept_place

LEAD = 'An opening paragraph long enough to fill a side.\n\n'


def kept(text: str, quote: str, nth: int = 0) -> dict:
    """What a draft keeps of its passage: the quote, its UTF-16 start and its sides, as the browser takes them."""
    start = -1
    for _ in range(nth + 1):
        start = text.index(quote, start + 1)
    from16 = _u16(text[:start])
    prefix, suffix = anchor_context(text, from16, from16 + _u16(quote), quote)
    return {'quote': quote, 'from16': from16, 'prefix': prefix, 'suffix': suffix}


def place(text: str, draft: dict):
    return kept_place(text, draft['quote'], from16=draft['from16'], prefix=draft['prefix'], suffix=draft['suffix'])


def span(text: str, quote: str, nth: int = 0) -> tuple[int, int]:
    start = -1
    for _ in range(nth + 1):
        start = text.index(quote, start + 1)
    return _u16(text[:start]), _u16(text[:start]) + _u16(quote)


def test_unchanged_passage_is_placed_where_it_was():
    text = LEAD + 'The launch moves to Friday.\nNotes follow.\n'
    assert place(text, kept(text, 'Friday')) == span(text, 'Friday')


def test_a_repeated_word_goes_to_the_copy_it_was_written_on():
    text = LEAD + 'First, ship it.\n\nSecond, ship it again after review.\n'
    assert place(text, kept(text, 'ship', 1)) == span(text, 'ship', 1)


def test_two_copies_with_the_same_sides_equally_near_are_refused():
    filler = 'ab' * 20
    text = filler + 'word' + filler + 'word' + filler
    draft = kept(text, 'word')
    first, second = span(text, 'word')[0], span(text, 'word', 1)[0]
    assert place(text, draft) == span(text, 'word')
    assert place(text, {**draft, 'from16': (first + second) // 2}) is None


def test_a_deleted_passage_is_refused():
    text = LEAD + 'Keep this line.\nDrop this line.\nEnd.\n'
    draft = kept(text, 'Drop this line.')
    assert place(text.replace('Drop this line.\n', ''), draft) is None


def test_an_edit_on_either_side_is_refused_even_though_the_words_remain():
    text = LEAD + 'Budget is final for Q3.\n'
    draft = kept(text, 'final')
    assert place(text.replace('Budget is', 'Budget was'), draft) is None
    assert place(text.replace('for Q3', 'for Q4'), draft) is None


def test_an_edit_away_from_the_passage_moves_it_with_its_text():
    text = LEAD + 'Budget is final for Q3.\n'
    draft = kept(text, 'final')
    edited = 'A new first line added well before.\n' + text
    assert place(edited, draft) == span(edited, 'final')
    assert place(edited, draft) != (draft['from16'], draft['from16'] + 5)


def test_a_short_side_must_still_reach_the_start_of_the_document():
    text = 'Title words\n' + LEAD
    draft = kept(text, 'Title')
    assert draft['prefix'] == ''
    assert place(text, draft) == (0, 5)
    assert place('# ' + text, draft) is None


def test_offsets_count_utf16_units_past_an_emoji():
    text = LEAD + 'Party 🎉 starts at noon.\n'
    edited = '🎉🎉 ' + 'more words to move it along, past the side.\n' + text
    assert place(edited, kept(text, 'noon')) == span(edited, 'noon')


TABLE = LEAD + '| Plan | Ready |\n| --- | --- |\n| Alpha | Yes |\n| Beta | Yes |\n| Gamma | No |\n'


def test_a_table_cell_is_placed_on_its_own_row():
    draft = kept(TABLE, 'Yes', 1)
    assert place(TABLE, draft) == span(TABLE, 'Yes', 1)
    edited = 'Added above the table, far enough away.\n' + TABLE
    assert place(edited, draft) == span(edited, 'Yes', 1)


def test_a_repeated_cell_word_whose_row_was_deleted_is_refused_not_moved_to_the_other_row():
    draft = kept(TABLE, 'Yes', 1)
    without_row = TABLE.replace('| Beta | Yes |\n', '')
    assert 'Yes' in without_row
    assert place(without_row, draft) is None


def test_an_empty_cell_is_placed_on_its_own_cell_source():
    table = LEAD + '| Plan | Owner |\n| --- | --- |\n| Alpha |   |\n| Beta | Sam |\n'
    start = table.index('|   |') + 1
    draft = {'quote': '   ', 'from16': _u16(table[:start]),
             'prefix': table[max(0, start - 32):start], 'suffix': table[start + 3:start + 35]}
    assert place(table, draft) == (draft['from16'], draft['from16'] + 3)
    assert place(table.replace('| Alpha |   |', '| Alpha | Lee |'), draft) is None


def test_nothing_to_place_without_a_quote_or_a_start():
    text = LEAD + 'words\n'
    draft = kept(text, 'words')
    assert place(text, {**draft, 'quote': ''}) is None
    assert place(text, {**draft, 'from16': None}) is None
    assert place(text, {**draft, 'from16': True}) is None
