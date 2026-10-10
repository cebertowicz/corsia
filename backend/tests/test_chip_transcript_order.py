"""Live notices must carry the durable position that a history reload reads."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.queries import insert_message

UNBOUND = {'turn_unbound_reason': 'legacy_import'}


def seed_thread(db):
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
        "VALUES ('chip-parent','u-test','Parent','2026-01-01','2026-01-01')"
    )
    db.commit()
    insert_message('chip-parent', 'assistant', 'Before', UNBOUND, db=db)


def assert_history_order(db, events):
    notices = [payload['message'] for _, payload in events
               if payload['type'] == 'message_added']
    assert len(notices) == 1
    live = notices[0]
    after = insert_message('chip-parent', 'assistant', 'After', UNBOUND, db=db)
    saved = db.execute('SELECT * FROM messages WHERE id=?', (live['id'],)).fetchone()
    later = db.execute('SELECT seq FROM messages WHERE id=?', (after,)).fetchone()
    assert live.get('seq') == saved['seq']
    assert 0 < live['seq'] < later['seq']
    assert live['created_at'] == saved['created_at']
    assert live['delivery_state'] == saved['delivery_state'] == 'delivered'


@pytest.mark.parametrize('kind', ['spawn', 'boundary', 'done', 'cc-exit', 'codex-exit'])
def test_runtime_notices_keep_their_history_position(deleg_db, kind):
    seed_thread(deleg_db)
    events = []
    broadcast = lambda thread, payload: events.append((thread, payload))
    if kind == 'spawn':
        from subagents.cc_runtime_markers import post_subagent_marker
        post_subagent_marker('chip-parent', 'child', 'Worker', broadcast, deleg_db)
    elif kind == 'boundary':
        from subagents.cc_event_simple import record_tool_boundary_violation
        record_tool_boundary_violation('chip-parent', broadcast, deleg_db,
                                       {'type': 'harness_error'}, 'Boundary')
    elif kind == 'done':
        from subagents.cc_event_result import notify_parent
        notify_parent(SimpleNamespace(parent_thread_id='chip-parent'), 'child',
                      'Done', broadcast, deleg_db)
    else:
        if kind == 'cc-exit':
            from subagents.cc_runtime_errors import surface_exit_error
            options = {}
        else:
            from subagents.codex_runtime_errors import surface_exit_error
            options = {'emit_terminal': False}
        surface_exit_error('chip-parent', 1, 'Failed', broadcast, deleg_db, None,
                           insert_message_fn=insert_message,
                           logger=logging.getLogger(__name__), **options)
    assert_history_order(deleg_db, events)


def test_artifact_notice_keeps_its_history_position(deleg_db, monkeypatch):
    from registry.removal_events import emit_artifact_removal_event, broadcast_artifact_removal_event
    seed_thread(deleg_db)
    events = []
    monkeypatch.setattr('infra.state.broadcast_to_thread',
                        lambda thread, payload: events.append((thread, payload)))
    payload = emit_artifact_removal_event(
        thread_id='chip-parent', artifact_id='artifact', artifact_type='document',
        title='Document', subtype='hidden_from_pane', actor_kind='user', db=deleg_db,
    )
    assert events == []
    deleg_db.commit()
    broadcast_artifact_removal_event(payload)
    assert_history_order(deleg_db, events)


@pytest.mark.asyncio
@pytest.mark.parametrize('shared_connection', [False, True])
async def test_codex_marker_keeps_its_history_position(deleg_db, shared_connection):
    from subagents.codex_runtime_events import record_terminal_marker
    seed_thread(deleg_db)
    events = []
    await record_terminal_marker(
        'chip-parent', 'Stopped', {'type': 'cc_error'},
        lambda thread, payload: events.append((thread, payload)),
        db=deleg_db if shared_connection else None,
    )
    assert_history_order(deleg_db, events)


@pytest.mark.asyncio
async def test_send_receipt_keeps_its_history_position(deleg_db, monkeypatch):
    from infra.delegation_markers import emit_send_ref_marker
    seed_thread(deleg_db)
    events = []
    monkeypatch.setattr('infra.state.broadcast_to_thread',
                        lambda thread, payload: events.append((thread, payload)))
    await emit_send_ref_marker('chip-parent', 'target', 'Worker', 'subagent')
    assert_history_order(deleg_db, events)
