import pytest

from doceditor import snapshot_reanchor
from doceditor.quote_anchor import anchor_context


SOURCE = 'Heading\n\nBefore First line here.\nSecond line here.\nThird line here. After\n\nEnd\n'


def resolve(source, current, quote, *, start=None, replacement=None):
    start = source.index(quote) if start is None else start
    end = start + len(quote)
    prefix, suffix = anchor_context(source, start, end, quote)
    comment = {'id': 'c', 'parent_id': None, 'review': False, 'decision': None,
               'replacement': replacement, 'anchor_text': quote, 'from_off': start,
               'to_off': end, 'anchor_prefix': prefix, 'anchor_suffix': suffix,
               'snapshot_id': 1}
    snapshot_reanchor.resolve_versioned([comment], current, versions={1: source})
    return comment


@pytest.mark.parametrize('quote,replacement', [
    ('line here.\nSecond', None),
    ('First line here.\nSecond line here.\nThird', None),
    ('First line here.\nSecond line here.\n', None),
    ('First line here.\nSecond line here.', 'replacement'),
])
def test_unchanged_multiline_anchors(quote, replacement):
    comment = resolve(SOURCE, SOURCE, quote, replacement=replacement)
    assert not comment['orphaned']
    assert SOURCE[comment['from_off']:comment['to_off']] == quote


@pytest.mark.parametrize('current', [
    'New heading\n' + SOURCE,
    SOURCE + 'New footer\n',
])
def test_edits_outside_multiline_anchor(current):
    quote = 'First line here.\nSecond line here.\nThird'
    comment = resolve(SOURCE, current, quote)
    assert not comment['orphaned']
    assert current[comment['from_off']:comment['to_off']] == quote


@pytest.mark.parametrize('current', [
    SOURCE.replace('Second line here.', 'Second changed.'),
    SOURCE.replace('Second line here.\n', ''),
    SOURCE.replace('Before First', 'Changed First'),
    SOURCE.replace('Third line here. After', 'Third line here. Changed'),
])
def test_multiline_anchor_orphans_when_contents_or_context_change(current):
    quote = 'First line here.\nSecond line here.\nThird line here.'
    assert resolve(SOURCE, current, quote)['orphaned']


def test_removed_duplicate_does_not_move_to_other_block():
    block = 'First line here.\nSecond line here.\n'
    source = 'head\n' + block + 'middle\n' + block + 'tail\n'
    current = 'head\nmiddle\n' + block + 'tail\n'
    comment = resolve(source, current, block, start=source.index(block))
    assert comment['orphaned']


def test_surviving_separated_duplicate_remains_detached():
    block = 'First line here.\nSecond line here.\n'
    source = 'head\n' + block + 'middle\n' + block + 'tail\n'
    current = 'head\nmiddle\n' + block + 'tail\n'
    comment = resolve(source, current, block, start=source.rindex(block))
    assert comment['orphaned']


def test_duplicate_replaced_elsewhere_with_same_count_is_ambiguous():
    block = 'First line here.\nSecond line here.\n'
    source = 'head\n' + block * 2 + 'middle\ntail\n'
    current = 'head\n' + block + 'middle\n' + block + 'tail\n'
    assert resolve(source, current, block)['orphaned']


@pytest.mark.parametrize('old_copies,new_copies', [(2, 1), (1, 2)])
def test_adjacent_duplicate_count_change_is_ambiguous(old_copies, new_copies):
    block = 'First line here.\nSecond line here.\n'
    source = 'head\n' + block * old_copies + 'tail\n'
    current = 'head\n' + block * new_copies + 'tail\n'
    # The same current text results whichever copy was removed or inserted.
    comment = resolve(source, current, block)
    assert comment['orphaned']
