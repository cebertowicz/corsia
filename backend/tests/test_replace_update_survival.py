"""Replacement keeps migration, comment admission and human detail contracts."""
import json

import pytest

from infra.database import get_db, write_transaction
from infra.input_admission import accept_thread_input, stamped_human_metadata
from infra.reply_context import RejectedComments
from presence_helpers import USER, new_thread, pdb
from tools.post_update import _persist_update

__all__ = ['pdb']


def save(thread, text='original', ids=()):
    return _persist_update(thread, text, None, None, 'turn', ids, 'updates' if ids else None)[0]


def comment(db, thread, seq):
    metadata = {'reply_context': {'comments': [{'id': 'c1', 'seq': seq, 'from_off': 0,
        'to_off': 8, 'quote': 'original', 'anchor_prefix': '', 'anchor_suffix': '', 'note': 'Keep my thought'}]}}
    with write_transaction(db, scope='replacement_comment_test'):
        stamped = stamped_human_metadata(db, 'user', 'question', metadata, thread_id=thread)
        return accept_thread_input(thread, role='user', content='', intent='apply_now',
                                   message_id='question', metadata=stamped, db=db)


@pytest.mark.parametrize('comment_first', [False, True])
def test_comment_and_replacement_serialize_on_two_connections(pdb, comment_first):
    thread = new_thread(pdb, 'comment-order')
    old = save(thread)
    seq = pdb.execute('SELECT seq FROM messages WHERE id=?', (old,)).fetchone()[0]
    other = get_db()
    try:
        if comment_first:
            comment(other, thread, seq)
            with pytest.raises(ValueError, match='person commented'):
                save(thread, 'replacement', (old,))
            assert pdb.execute('SELECT COUNT(*) FROM chat_comments WHERE target_message_id=?', (old,)).fetchone()[0] == 1
        else:
            save(thread, 'replacement', (old,))
            with pytest.raises(RejectedComments) as failure:
                comment(other, thread, seq)
            assert failure.value.cause == 'comment_quote_gone'
            assert pdb.execute("SELECT COUNT(*) FROM messages WHERE id='question'").fetchone()[0] == 0
            assert pdb.execute('SELECT COUNT(*) FROM chat_comments').fetchone()[0] == 0
    finally:
        other.close()


def test_legacy_migration_registers_only_marked_assistant_updates_as_unknown(pdb):
    from infra.migration_194_to_195_update_replacements import migrate_194_to_195
    thread = new_thread(pdb, 'migration')
    old = save(thread)
    ordinary = save(thread, 'ordinary')
    pdb.execute('DELETE FROM thread_updates WHERE source_id=?', (ordinary,))
    pdb.execute('DELETE FROM message_update_nodes')
    pdb.execute('DELETE FROM message_update_state')
    migrate_194_to_195(pdb)
    migrate_194_to_195(pdb)  # normal migration operations are safe when the schema already exists
    pdb.commit()
    rows = pdb.execute('SELECT message_id,update_kind,presence_at_post,seen_reason FROM message_update_state').fetchall()
    assert [tuple(row) for row in rows] == [(old, 'legacy_update', 'unknown', 'unknown')]
    with pytest.raises(ValueError, match='unknown.*counts as seen'):
        save(thread, 'replacement', (old,))


def test_thread_delete_cleans_auxiliary_rows_without_foreign_keys(pdb):
    from infra.thread_updates import delete_thread_updates
    thread = new_thread(pdb, 'cleanup')
    old = save(thread)
    pdb.execute('INSERT INTO message_update_nodes VALUES (?,?)', (old, 'task'))
    pdb.commit()
    delete_thread_updates(pdb, thread)
    assert pdb.execute('SELECT COUNT(*) FROM message_update_state WHERE thread_id=?', (thread,)).fetchone()[0] == 0
    assert pdb.execute('SELECT COUNT(*) FROM message_update_nodes').fetchone()[0] == 0


def test_replacing_an_answering_update_keeps_its_comment_answer_and_read_stamp(pdb):
    from infra.chat_comment_index import attach_comments

    thread = new_thread(pdb, 'answer-survival')
    target = save(thread)
    seq = pdb.execute('SELECT seq FROM messages WHERE id=?', (target,)).fetchone()[0]
    comment(pdb, thread, seq)
    from thread.transcript_order import draw_transcript_position
    question_seq = draw_transcript_position(pdb, thread)
    pdb.execute("UPDATE messages SET seq=?,delivery_state='delivered' WHERE id='question'", (question_seq,))
    pdb.commit()
    old = save(thread, '[c1.1] retained answer\n\nOther news.')
    targets = [{'id': target, 'role': 'assistant'}]
    attach_comments(pdb, thread, USER, targets)
    before = targets[0]['chat_comments']
    new = save(thread, 'Current news.', (old,))
    targets = [{'id': target, 'role': 'assistant'}]
    attach_comments(pdb, thread, USER, targets)
    assert targets[0]['chat_comments'] == before
    answers = [answer for root in before for answer in root['answers']]
    assert [answer['assistant_message_id'] for answer in answers] == [old]
    assert pdb.execute('SELECT content FROM messages WHERE id=?', (old,)).fetchone()[0].startswith('[c1.1] retained answer')
    assert pdb.execute('SELECT replaced_by FROM message_update_state WHERE message_id=?', (old,)).fetchone()[0] == new


@pytest.mark.parametrize('name,args', [
    ('post_update', {'text': 'private'}),
    ('mcp__corsia__post_update', {'text': 'private'}),
    ('tool_call', json.dumps({'name': 'post_update', 'arguments': {'text': 'private'}})),
    ('mcp__corsia__tool_call', {'name': 'post_update', 'arguments': {'text': 'private'}}),
])
def test_stored_human_tool_details_hide_every_update_without_touching_other_calls(name, args):
    from api.post_update_details import hide_update_details
    call = {'name': name, 'args': args, 'result': 'private receipt', 'error_line': 'private', 'result_state': 'pending'}
    untouched = {'name': 'read_file', 'args': {'path': 'public'}, 'result': 'file'}
    rows = [{'metadata': {'tool_calls': [call, untouched.copy()]}}]
    hide_update_details(rows)
    assert call == {'name': name, 'args': '', 'result': '', 'result_state': 'loaded'}
    assert rows[0]['metadata']['tool_calls'][1] == untouched
