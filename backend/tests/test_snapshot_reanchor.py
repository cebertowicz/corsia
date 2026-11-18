import sqlite3

from doceditor import comment_anchoring, comment_snapshots, snapshot_reanchor
from doceditor.quote_anchor import anchor_context


def row(text, quote, start):
    end = start + len(quote)
    prefix, suffix = anchor_context(text, start, end, quote)
    return {'id': 'c', 'parent_id': None, 'review': False, 'decision': None,
            'replacement': None, 'anchor_text': quote, 'from_off': start, 'to_off': end,
            'anchor_prefix': prefix, 'anchor_suffix': suffix}


def test_direct_span_mapping_matches_crdt_for_edits_and_utf16_boundaries():
    cases = [
        ('hello world', 'hello brave world', [(0, 5), (6, 11), (5, 5)]),
        ('a😀 target\nlast', 'a😀 changed\nlast!', [(1, 3), (4, 10), (0, 13)]),
        ('one\ntwo\nthree\n', 'one\nthree\n', [(4, 7), (8, 13)]),
        ('abc', 'aXYZc', [(1, 2), (0, 3), (2, 2)]),
    ]
    for old, new, spans in cases:
        doc = snapshot_reanchor.live_text.create(old)
        text = doc['text']
        state = {'spans': spans, 'before': old, 'open': set()}
        def changed(event):
            state['spans'], state['open'] = snapshot_reanchor.live_text.follow_spans(
                state['spans'], state['before'], event.delta, state['open'])
            state['before'] = str(text)
        observer = text.observe(changed)
        try:
            snapshot_reanchor.live_text.apply_text(doc, new)
        finally:
            text.unobserve(observer)
        assert snapshot_reanchor._map_spans(old, new, spans) == state['spans']


def test_versioned_rows_resolve_from_their_own_source_and_legacy_uses_existing_rules():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE doc_comment_snapshots (id INTEGER PRIMARY KEY, digest BLOB NOT NULL, text BLOB NOT NULL)')
    db.execute('BEGIN')
    old = 'heading\n- keep this line\n- tail\n'
    middle = 'heading\n- inserted\n- keep this line\n- tail\n'
    new = 'heading\n- inserted\n- keep this line\n- tail changed\n'
    first_id = comment_snapshots.intern(db, old)
    second_id = comment_snapshots.intern(db, middle)
    first = row(old, 'keep this line', old.index('keep'))
    second = row(middle, 'keep this line', middle.index('keep'))
    legacy = row(old, 'keep this line', old.index('keep'))
    first.update(id='first', snapshot_id=first_id)
    second.update(id='second', snapshot_id=second_id)
    legacy.update(id='legacy', snapshot_id=None)
    legacy_baseline = dict(legacy)
    baseline_updates = comment_anchoring.resolve_threads([legacy_baseline], new)
    legacy_updates, versioned_updates, invalid = snapshot_reanchor.resolve_versioned([first, second, legacy], new, db)
    assert not invalid
    assert all(not item['orphaned'] for item in (first, second))
    assert all(new[item['from_off']:item['to_off']] == 'keep this line' for item in (first, second))
    assert legacy == legacy_baseline and legacy_updates == baseline_updates
    assert {item['id']: item['expected_snapshot_id'] for item in versioned_updates} == {
        'first': first_id, 'second': second_id,
    }
    db.close()


def test_mismatched_snapshot_uses_legacy_resolution_and_reports_reason(caplog):
    caplog.set_level('INFO', logger='doceditor.snapshot_reanchor')
    source = 'old b text\n'
    current = 'prefix old b text\n'
    comment = row(source, 'b', source.index('b'))
    comment.update(id='mismatched', snapshot_id=1, from_off=10, to_off=11)
    baseline = dict(comment)
    expected_updates = comment_anchoring.resolve_threads([baseline], current)
    legacy, versioned, invalid = snapshot_reanchor.resolve_versioned([comment], current, versions={1: source})
    assert [item['id'] for item in invalid] == ['mismatched']
    assert comment == baseline
    assert legacy == expected_updates and versioned == []
    assert 'mismatched' in caplog.text and 'quote_mismatch' in caplog.text


def test_restored_duplicate_attaches_and_multiline_uses_existing_rule(monkeypatch):
    old = 'header\ntarget\nseparator\ntarget\nend\n'
    deleted = 'header\ntarget\nend\n'
    duplicate = row(old, 'target', old.index('target'))
    assert snapshot_reanchor.resolve([duplicate], old, deleted) == []
    assert duplicate['orphaned']
    restored = row(old, 'target', old.index('target'))
    snapshot_reanchor.resolve([restored], old, old)
    assert not restored['orphaned'] and restored['from_off'] == old.index('target')

    calls = []
    apply_text = snapshot_reanchor.live_text.apply_text
    def tracked(*args, **kwargs):
        calls.append(1)
        return apply_text(*args, **kwargs)
    monkeypatch.setattr(snapshot_reanchor.live_text, 'apply_text', tracked)
    multi = row(old, 'target\nseparator', old.index('target'))
    snapshot_reanchor.resolve([multi], old, old)
    assert calls == [1]
    assert multi['orphaned']  # The existing rule rejects a span across lines.


def test_surviving_line_moves_with_insertion():
    old = 'heading\n- keep this line\n- another line\n'
    new = 'heading\n- inserted line\n- keep this line\n- another line\n'
    comment = row(old, 'keep this line', old.index('keep'))
    updates = snapshot_reanchor.resolve([comment], old, new)
    assert not comment['orphaned']
    assert new[comment['from_off']:comment['to_off']] == 'keep this line'
    assert updates and updates[0]['id'] == 'c'


def test_survivor_is_returned_for_version_advance_even_if_offsets_and_context_are_unchanged():
    old = 'keep this line\n' + 'x' * 80 + '\nold tail\n'
    new = old.replace('old tail', 'new tail')
    comment = row(old, 'keep this line', 0)
    updates = snapshot_reanchor.resolve([comment], old, new)
    assert not comment['orphaned']
    assert len(updates) == 1 and updates[0]['id'] == 'c'
    assert (updates[0]['from_off'], updates[0]['to_off']) == (0, len('keep this line'))


def test_deleted_line_keeps_quote_and_stored_anchor_evidence():
    old = 'heading\n- deleted target\n- other content\n'
    new = 'heading\n- other content\n'
    comment = row(old, 'deleted target', old.index('deleted'))
    before = dict(comment)
    updates = snapshot_reanchor.resolve([comment], old, new)
    assert comment['orphaned']
    assert comment['from_off'] == comment['to_off'] == new.index('- other')
    assert comment['anchor_text'] == before['anchor_text']
    assert comment['anchor_prefix'] == before['anchor_prefix']
    assert comment['anchor_suffix'] == before['anchor_suffix']
    assert updates == []


def test_two_edits_keep_the_same_anchor_when_each_version_is_saved():
    old = 'heading\n- keep this line\n- tail\n'
    middle = 'heading\n- inserted\n- keep this line\n- tail\n'
    new = 'heading\n- inserted\n- keep this line expanded\n- tail\n'
    comment = row(old, 'keep this line', old.index('keep'))
    snapshot_reanchor.resolve([comment], old, middle)
    assert not comment['orphaned']
    snapshot_reanchor.resolve([comment], middle, new)
    assert not comment['orphaned']
    assert new[comment['from_off']:comment['to_off']].startswith('keep this line')


def test_edited_line_follows_when_dev_selects_the_same_range():
    old = 'Heading\n\n| Tool servers you add | What the agent passes to the tool | When an agent calls the tool | Remove the app |\n\nEnd\n'
    new = old.replace('Tool servers', 'Connected apps')
    quote = old.splitlines()[2]
    comment = row(old, quote, old.index(quote))
    legacy = dict(comment)
    comment_anchoring.resolve_threads([legacy], new)
    assert not legacy['orphaned']
    snapshot_reanchor.resolve([comment], old, new)
    assert not comment['orphaned']
    assert (comment['from_off'], comment['to_off']) == (legacy['from_off'], legacy['to_off'])
    assert comment['anchor_text'] == new[comment['from_off']:comment['to_off']]


def test_edited_suggestion_keeps_quote_when_dev_agrees_on_range():
    quote = '| Tool servers you add | What the agent passes to the tool | When an agent calls the tool | Remove the app |'
    old = 'Heading\n\n' + quote + '\n\nEnd\n'
    new = old.replace('Tool servers', 'Connected apps')
    suggestion = row(old, quote, old.index(quote))
    suggestion['replacement'] = 'Suggested replacement'
    legacy = dict(suggestion)
    comment_anchoring.resolve_threads([legacy], new)
    assert not legacy['orphaned']
    snapshot_reanchor.resolve([suggestion], old, new)
    assert not suggestion['orphaned']
    assert (suggestion['from_off'], suggestion['to_off']) == (legacy['from_off'], legacy['to_off'])
    assert suggestion['anchor_text'] == quote
    assert suggestion['text_changed'] is True


def test_edited_line_goes_when_dev_selects_a_different_range():
    quote = 'Anyone who can push to the branch you track can change what your install runs on its next update.'
    old = 'Heading\n\n' + quote + '\n\nEnd\n'
    new = old.replace(quote, quote.replace('your install', 'a source install') + '\n\n**Desktop app**: unrelated extra paragraph.')
    comment = row(old, quote, old.index(quote))
    comment['body'] = 'Preserve this.'
    legacy = dict(comment)
    comment_anchoring.resolve_threads([legacy], new)
    assert not legacy['orphaned']
    updates = snapshot_reanchor.resolve([comment], old, new)
    assert (comment['from_off'], comment['to_off']) != (legacy['from_off'], legacy['to_off'])
    assert comment['orphaned']
    assert (comment['anchor_text'], comment['body']) == (quote, 'Preserve this.')
    assert updates == []


def test_group_preserves_deleted_and_moved_anchors_independently():
    old = 'heading\n- keep this line\n- deleted target\n'
    new = 'heading\n- inserted\n- keep this line\n'
    kept = row(old, 'keep this line', old.index('keep'))
    deleted = row(old, 'deleted target', old.index('deleted'))
    deleted['id'] = 'deleted'
    updates = snapshot_reanchor.resolve([kept, deleted], old, new)
    assert not kept['orphaned'] and deleted['orphaned']
    assert new[kept['from_off']:kept['to_off']] == 'keep this line'
    assert deleted['anchor_text'] == 'deleted target'
    assert {item['id'] for item in updates} == {'c'}


def test_group_keeps_unique_line_but_detaches_ambiguous_duplicate():
    old = 'header\ntarget\nseparator\ntarget\nunique line\n'
    new = 'header\ntarget\nunique line\n'
    duplicate = row(old, 'target', old.index('target'))
    unique = row(old, 'unique line', old.index('unique line'))
    unique['id'] = 'unique'
    updates = snapshot_reanchor.resolve([duplicate, unique], old, new)
    assert duplicate['orphaned']
    assert not unique['orphaned']
    assert new[unique['from_off']:unique['to_off']] == 'unique line'
    assert {item['id'] for item in updates} == {'unique'}


def test_invalid_stored_span_does_not_search_for_another_occurrence():
    old = '🐈 target\n'
    comment = row(old, 'target', 3)
    comment['from_off'] = 1  # Inside the surrogate pair.
    before = dict(comment)
    assert snapshot_reanchor.resolve([comment], old, old) == []
    assert comment['orphaned']
    assert {k: comment[k] for k in before} == before


def test_utf16_offsets_survive_insertion_before_anchor():
    old = '🐈 heading\n- keep this line\n'
    new = '🐈 heading\n- inserted\n- keep this line\n'
    start = len(old[:old.index('keep')].encode('utf-16-le')) // 2
    comment = row(old, 'keep this line', start)
    snapshot_reanchor.resolve([comment], old, new)
    encoded = new.encode('utf-16-le')
    assert not comment['orphaned']
    assert encoded[comment['from_off'] * 2:comment['to_off'] * 2].decode('utf-16-le') == 'keep this line'
