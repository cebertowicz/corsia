"""Batch reads preserve the exact per-range message/card projection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN

_AUTH = {"token": OWNER_TOKEN}


@pytest.mark.integration
def test_batch_matches_individual_ranges_before_and_after_file_registration(integration_client):
    from infra import config
    from infra.database import get_db
    from registry import service as registry_service

    client = integration_client
    thread = client.post('/api/threads', params=_AUTH, json={'title': 'card batch'}).json()['thread_id']
    path = Path(config.WORKSPACE) / f'card-batch-{thread}.md'
    path.write_text('file card', encoding='utf-8')
    db = get_db()
    ids = [f'{thread}-m{i}' for i in range(3)]
    try:
        for index, mid in enumerate(ids):
            db.execute(
                'INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?,?,?,?,?,?,?)',
                (mid, thread, 'assistant', path.name, json.dumps({'for_user': True}), f'2026-01-01T00:00:0{index}', index + 1),
            )
        db.commit()

        def compare():
            old = []
            for mid in ids:
                response = client.get(f'/api/threads/{thread}/messages', params={**_AUTH, 'limit': 50, 'from_id': mid, 'to_id': mid})
                assert response.status_code == 200
                old.extend(response.json()['messages'])
            response = client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={'message_ids': ids})
            assert response.status_code == 200, response.text
            assert response.json()['messages'] == old
            return old

        before = compare()
        assert all(row['cards'][0]['artifact_id'] is None for row in before)
        artifact = registry_service.register_write_strict(thread, str(path), title='Registered', db=db)['id']
        after = compare()
        assert all(row['cards'][0]['artifact_id'] == artifact for row in after)
        db.execute('UPDATE artifact_identities SET title=? WHERE id=?', ('Renamed', artifact))
        db.commit()
        renamed = compare()
        assert all(row['cards'][0]['title'] == 'Renamed' for row in renamed)
        db.execute('UPDATE artifact_identities SET state=? WHERE id=?', ('retired', artifact))
        db.commit()
        retired = compare()
        assert all(not row.get('cards') for row in retired)

        individual = client.get(f'/api/threads/{thread}/messages', params={
            **_AUTH, 'limit': 50, 'from_id': ids[1], 'to_id': ids[2],
        }).json()['messages']
        batched = client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={
            'message_ids': [ids[0]], 'turn_range': {'from_id': ids[1], 'to_id': ids[2]},
        }).json()['messages']
        assert batched[:len(individual)] == individual
    finally:
        db.close()
        path.unlink(missing_ok=True)


@pytest.mark.integration
def test_batch_matches_ask_placement_and_hold_clearance(integration_client):
    import uuid
    from infra import config
    from infra.database import get_db
    from plans import service
    from plans.boundary import PlanActor, mutate
    from registry import service as registry_service
    from registry.attention import clear_attention

    client = integration_client
    thread = client.post('/api/threads', params=_AUTH, json={'title': 'card ask batch'}).json()['thread_id']
    path = Path(config.WORKSPACE) / f'card-ask-{thread}.md'
    path.write_text('ask', encoding='utf-8')
    db = get_db()
    mid = f'{thread}-step'
    try:
        user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread,)).fetchone()['user_id']
        metadata = {'turn_id': 'turn-1', 'tool_calls': [{'id': 'prov-1', 'type': 'function', 'function': {'name': 'plan_update', 'arguments': '{}'}}]}
        db.execute(
            'INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?,?,?,?,?,?,?)',
            (mid, thread, 'assistant', path.name, json.dumps(metadata), '2026-01-01T00:00:00', 100),
        )
        db.commit()
        artifact = registry_service.register_write_strict(thread, str(path), title='Ask', db=db)['id']
        plan = service.ensure_plan('thread', thread, db=db)
        node = service.add_node(plan.id, kind='task', title='Review', acceptance_criteria='done', db=db)
        db.execute("UPDATE plan_nodes SET assignee='subagent' WHERE id=?", (node.id,))
        db.commit()

        def compare():
            old = client.get(f'/api/threads/{thread}/messages', params={**_AUTH, 'limit': 50, 'from_id': mid, 'to_id': mid}).json()['messages']
            response = client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={'message_ids': [mid]})
            assert response.status_code == 200, response.text
            assert response.json()['messages'] == old
            return old[0]['cards']

        assert [card['kind'] for card in compare()] == ['artifact']
        mutate(
            plan.id, 'block_node',
            {'node_id': node.id, 'reason_key': 'needs_review', 'reason': 'read it', 'owner_kind': 'user',
             'ask_artifact_id': artifact, 'expected_revision': service._node_row(db, node.id)['revision']},
            actor=PlanActor(thread, user_id, None, turn_id='turn-1', provider_call_id='prov-1'),
            provenance='test.ask', mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        placed = compare()
        assert any(card['kind'] == 'ask' and card['state'] == 'needs_review' for card in placed)
        clear_attention(artifact, outcome='answer', db=db)
        db.commit()
        cleared = compare()
        assert any(card['kind'] == 'ask' and card['state'] == 'answered' for card in cleared)
    finally:
        db.close()
        path.unlink(missing_ok=True)


@pytest.mark.integration
def test_batch_read_empty_for_one_bad_range_keeps_the_other_ranges(integration_client):
    """One stale range must not cost the cards of the messages beside it."""
    from infra.database import get_db

    client = integration_client
    thread = client.post('/api/threads', params=_AUTH, json={'title': 'card batch bad range'}).json()['thread_id']
    db = get_db()
    ids = [f'{thread}-m{i}' for i in range(4)]
    try:
        for index, mid in enumerate(ids):
            db.execute(
                'INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?,?,?,?,?,?,?)',
                (mid, thread, 'assistant', f'body {index}', json.dumps({'for_user': True}), f'2026-01-01T00:00:0{index}', index + 1),
            )
        db.commit()

        def individual(from_id: str, to_id: str):
            response = client.get(f'/api/threads/{thread}/messages', params={
                **_AUTH, 'limit': 50, 'from_id': from_id, 'to_id': to_id,
            })
            return response.status_code, response.json().get('messages')

        # The separate ranged reads' own answers for the same two bad inputs.
        assert individual('no-such-message', 'no-such-message') == (200, [])
        assert individual(ids[3], ids[1])[0] == 400

        response = client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={
            'message_ids': [ids[0], 'no-such-message', ids[2]],
            'turn_range': {'from_id': ids[3], 'to_id': ids[1]},
        })
        assert response.status_code == 200, response.text
        assert response.json()['messages'] == [
            *individual(ids[0], ids[0])[1], *individual(ids[2], ids[2])[1],
        ]
    finally:
        db.close()


@pytest.mark.integration
def test_batch_requires_thread_access_and_rejects_too_many_ids(integration_client):
    client = integration_client
    assert client.post('/api/threads/nonexistent/messages/refresh', params=_AUTH, json={'message_ids': ['x']}).status_code == 404
    thread = client.post('/api/threads', params=_AUTH, json={'title': 'card batch limit'}).json()['thread_id']
    assert client.post(f'/api/threads/{thread}/messages/refresh', params={'token': OTHER_TOKEN}, json={'message_ids': ['x']}).status_code == 404
    response = client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={'message_ids': [str(i) for i in range(13)]})
    assert response.status_code == 400
