"""Chat comments are independent correction inputs alongside typed replies."""
import json

import pytest

from tests.test_corrections_recording import _db, _seed, _version


@pytest.mark.asyncio
async def test_real_admission_queues_committed_comment_identity(isolated_db, monkeypatch):
    from infra import send_service
    from jobs.corrections_recording import candidate_for_source
    from test_reply_context import _comment

    db = _db()
    _seed(db)
    db.execute("UPDATE messages SET seq=5 WHERE id='prior'")
    db.commit()
    row = dict(db.execute("SELECT * FROM threads WHERE id='thread'").fetchone())
    async def deliver(*args, **kwargs):
        return None
    monkeypatch.setattr(send_service, '_deliver_admission', deliver)
    monkeypatch.setattr('jobs.corrections_recording._plans_hear', lambda *args: None)
    seen = []
    monkeypatch.setattr('jobs.corrections_realtime.notify', lambda *args: seen.append(args))
    await send_service.send_thread_input('thread', row, 'plain', input_id='plain', native_mode='ws', ws=object())
    seen.clear()
    comments = [_comment(note=' '), _comment(id='key-2', note='Fix it')]
    await send_service.send_thread_input('thread', row, '', input_id='comment-send', native_mode='ws', ws=object(), metadata={'reply_context': {'comments': comments}})
    stored = db.execute("SELECT cid,user_message_id FROM chat_comments WHERE user_message_id='comment-send' ORDER BY cid").fetchall()
    assert len(stored) == 2 and not stored[1]['cid'].startswith('c1.')
    assert seen == [('chat_comment', f"{stored[1]['cid']}:{stored[1]['user_message_id']}")]
    candidate = candidate_for_source(*seen[0])
    assert candidate and candidate.content == 'Fix it' and candidate.arrived
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["", "Typed correction."])
async def test_chat_comment_candidates(isolated_db, typed):
    from jobs.corrections_recording import next_pending_batch

    db = _db()
    _seed(db)
    comments = [
        {"cid": "c1.1", "quote": "Agent first", "note": "Fix first"},
        {"cid": "c1.2", "quote": "Agent second", "note": "Fix second"},
        {"cid": "c1.3", "quote": "Agent third", "note": "  "},
    ]
    db.execute("UPDATE messages SET content=?,metadata=? WHERE id='owner-message'", (typed, json.dumps({"reply_context": {"comments": comments}})))
    for comment in comments:
        db.execute("INSERT INTO chat_comments (thread_id,target_message_id,user_message_id,cid,created_at) VALUES ('thread','prior','owner-message',?,'2026-10-01T00:01:00Z')", (comment['cid'],))
    db.commit()
    db.close()
    batch = await next_pending_batch(_version(), 20, None, since="2026-10-01T00:00:00Z")
    notes = [item for item in batch.candidates if item.source_kind == 'chat_comment']
    assert {item.content for item in notes} == {'Fix first', 'Fix second'}
    assert {item.context for item in notes} == {(('Agent', 'Agent first'),), (('Agent', 'Agent second'),)}
    assert len({item.source_id for item in notes}) == 2
    messages = [item for item in batch.candidates if item.source_kind == 'message']
    assert [item.content for item in messages] == ([typed] if typed else [])

    from core.corrections_recording import subject_key
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import _record_fact
    from core.correction_attribution import attributed_facts
    from core.lesson_corrections import correction_facts

    payload = {'label': 'correction', 'truncated': False, 'original_tokens': 5, 'failure_reason': 'none'}
    for item in notes:
        assert _record_fact(item, payload, _version())
    keys = [subject_key(item.source_kind, item.source_id) for item in notes]
    assert len(set(keys)) == 2
    db = _db()
    assert set(correction_facts(db, keys)) == set(keys)
    assert {fact.source_id for fact in attributed_facts(db) if fact.source_kind == 'chat_comment'} == {item.source_id for item in notes}
    db.close()
    again = await next_pending_batch(_version(), 20, None, since='2026-10-01T00:00:00Z')
    assert not [item for item in again.candidates if item.source_kind == 'chat_comment']
    assert weekly_correction_figure(_version(), since='2026-10-01T00:00:00Z', weeks=1) == 0


def test_send_comment_notification_requires_no_database(monkeypatch):
    from types import SimpleNamespace
    from jobs import corrections_recording

    seen = []
    monkeypatch.setattr('infra.database.get_db', lambda: pytest.fail('send hook read database'))
    monkeypatch.setattr('jobs.corrections_realtime.notify', lambda *args: seen.append(args))
    metadata = {'reply_context': {'comments': [{'cid': 'c1.1', 'note': 'Fix'}]}}
    corrections_recording.notify_if_owner(SimpleNamespace(message_id='m', duplicate=False, input_order=1), role='user', content='', metadata=metadata)
    assert seen == [('chat_comment', 'c1.1:m')]
    seen.clear()
    corrections_recording.notify_if_owner(SimpleNamespace(message_id='m', duplicate=False, input_order=1), role='user', content='[EVENT: system]', metadata=metadata)
    assert seen == []


@pytest.mark.asyncio
async def test_cursor_paging_and_owner_scope(isolated_db):
    from jobs.chat_comment_corrections import pending_chat_comment_keys
    from jobs.corrections_recording import next_pending_batch

    db = _db()
    _seed(db)
    metadata = {'reply_context': {'comments': [{'cid': 'c1.1', 'quote': 'q', 'note': 'fix'}]}}
    db.execute("UPDATE messages SET metadata=? WHERE id='owner-message'", (json.dumps(metadata),))
    db.execute("INSERT INTO chat_comments VALUES ('thread','prior','owner-message','c1.1','2026-10-01T00:01:00Z')")
    db.commit()
    db.close()
    assert len(pending_chat_comment_keys(_version(), 10, 'owner')) == 1
    assert pending_chat_comment_keys(_version(), 10, 'another-owner') == []
    first = await next_pending_batch(_version(), 1, None)
    key = first.selected_keys[0]
    assert key.source_kind == 'message'
    second = await next_pending_batch(_version(), 1, (key.created_at, key.source_kind, key.source_id))
    chat = second.selected_keys[0]
    assert chat.source_kind == 'chat_comment'
    third = await next_pending_batch(_version(), 1, (chat.created_at, chat.source_kind, chat.source_id))
    assert not third.selected_keys


def test_live_comment_arrival_and_non_owner_exclusion(isolated_db, monkeypatch):
    from types import SimpleNamespace
    from jobs import corrections_recording

    db = _db()
    _seed(db)
    metadata = {'reply_context': {'comments': [{'cid': 'c1.1', 'quote': 'Agent words', 'note': 'Fix it'}]}}
    db.execute('UPDATE messages SET content=?,metadata=? WHERE id=?', ('', json.dumps(metadata), 'owner-message'))
    db.execute("INSERT INTO chat_comments (thread_id,target_message_id,user_message_id,cid,created_at) VALUES ('thread','prior','owner-message','c1.1','2026-10-01T00:01:00Z')")
    db.commit()
    seen = []
    monkeypatch.setattr('jobs.corrections_realtime.notify', lambda kind, source: seen.append((kind, source)))
    accepted = SimpleNamespace(message_id='owner-message', duplicate=False, input_order=1)
    corrections_recording.notify_if_owner(accepted, role='user', content='', metadata=metadata)
    assert seen == [('chat_comment', 'c1.1:owner-message')]
    candidate = corrections_recording.candidate_for_source(*seen[0])
    assert candidate.arrived and candidate.content == 'Fix it'
    assert corrections_recording._record_fact(candidate, {'label': 'correction', 'truncated': False, 'original_tokens': 2, 'failure_reason': 'none'}, _version())
    notes = db.execute("SELECT summary FROM context_change_journal WHERE object_kind='thread_context'").fetchall()
    assert any('chat comment' in row[0] and 'Fix it' in row[0] for row in notes)
    db.execute('UPDATE messages SET metadata=? WHERE id=?', (json.dumps({**metadata, 'sender_kind': 'agent'}), 'owner-message'))
    db.commit()
    assert corrections_recording.candidate_for_source(*seen[0]) is None
    db.execute('UPDATE messages SET content=?,metadata=? WHERE id=?', ('[EVENT: system]', json.dumps(metadata), 'owner-message'))
    db.commit()
    assert corrections_recording.candidate_for_source(*seen[0]) is None
    db.close()
