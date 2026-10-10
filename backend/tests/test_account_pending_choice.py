"""An account a person chooses while a turn runs applies at the next turn, as a model choice does.

Pins: the choice is accepted during a running turn and the picker's next-turn account follows it;
the binding moves only at the next claim, through the one account mover; choosing the bound account
again cancels it; an account no longer permitted by then is dropped; an inactive thread still refuses.
"""
from __future__ import annotations

import json

import pytest

from connections import harness_usage
from connections.account_policy import AccountAdmissionError, turn_account
from connections.account_selection import rebind_idle_account
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _configured,
    _start,
    accounts,
    receipt_of,
    set_legacy_policy,
)


def _running(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch, enabled=False)
    harness_usage.record_allowed(provider='codex', receipt=receipt_of('codex', first['id'], db=db),
                                 source='discovery', db=db)
    _accept(db, 'input')
    assert _start(db, 'first-turn') is not None
    assert _bound(db) == (first['id'], None)
    return db, first, second


def _bound(db):
    row = db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                     "WHERE thread_id='destination' AND provider='codex'").fetchone()
    return row['account_id'], row['pending_account_id']


def _finish(db, turn):
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (turn,))
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE active_turn_id=?", (turn,))
    db.commit()


def _switches(db):
    rows = db.execute("SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchall()
    return [json.loads(row['metadata']) for row in rows]


def test_choice_during_a_running_turn_applies_at_the_next_turn(accounts, monkeypatch):
    db, first, second = _running(accounts, monkeypatch)
    chosen = rebind_idle_account('destination', second['id'], user_id='user', db=db)
    assert chosen == {'account_id': second['id'], 'runtime_context_epoch': 0, 'changed': False, 'pending': True}
    # The running turn keeps its account; the next turn is the chosen one.
    assert _bound(db) == (first['id'], second['id'])
    assert turn_account('codex', thread_id='destination', model='codex/test', db=db)[0] == second['id']
    assert _switches(db) == []
    _finish(db, 'first-turn')
    _accept(db, 'next')
    assert _start(db, 'second-turn') is not None
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='second-turn'").fetchone()[0] == second['id']
    assert _bound(db) == (second['id'], None)
    assert [item['reason'] for item in _switches(db)] == ['account_pinned']


def test_choosing_the_bound_account_again_cancels_the_choice(accounts, monkeypatch):
    db, first, second = _running(accounts, monkeypatch)
    rebind_idle_account('destination', second['id'], user_id='user', db=db)
    rebind_idle_account('destination', first['id'], user_id='user', db=db)
    assert _bound(db) == (first['id'], None)
    _finish(db, 'first-turn')
    _accept(db, 'next')
    assert _start(db, 'second-turn') is not None
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='second-turn'").fetchone()[0] == first['id']
    assert _switches(db) == []


def test_choice_no_longer_permitted_at_the_claim_is_dropped(accounts, monkeypatch):
    db, first, second = _running(accounts, monkeypatch)
    rebind_idle_account('destination', second['id'], user_id='user', db=db)
    set_legacy_policy('destination-project', 'codex', allowlist=[first['id']], default_account_id=first['id'], db=db)
    _finish(db, 'first-turn')
    _accept(db, 'next')
    assert _start(db, 'second-turn') is not None
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='second-turn'").fetchone()[0] == first['id']
    assert _bound(db) == (first['id'], None) and _switches(db) == []


@pytest.mark.parametrize('state', ['archived', 'paused'])
def test_inactive_thread_still_refuses_a_choice(accounts, monkeypatch, state):
    db, _first, second, _events = _configured(accounts, monkeypatch, enabled=False)
    db.execute("UPDATE threads SET lifecycle_state=? WHERE id='destination'", (state,))
    db.commit()
    with pytest.raises(AccountAdmissionError, match='account_change_requires_idle'):
        rebind_idle_account('destination', second['id'], user_id='user', db=db)


def test_the_picker_marks_the_held_choice_pending_until_the_claim(accounts, monkeypatch):
    from llm.account_catalog import project_accounts

    def selected():
        row = {'id': 'codex/test', 'litellm_model': 'codex/test', 'connection': 'codex', 'status': 'ready',
               'context_window': 999, 'capabilities': {}, 'resolved_identity': 'Default'}
        entries = project_accounts([row], thread_id='destination', db=db)[0]['runs_on']
        return [(entry['account_id'], entry.get('pending', False)) for entry in entries if entry['selected']]

    db, first, second = _running(accounts, monkeypatch)
    assert selected() == [(first['id'], False)]
    rebind_idle_account('destination', second['id'], user_id='user', db=db)
    assert selected() == [(second['id'], True)]
    _finish(db, 'first-turn')
    _accept(db, 'next')
    assert _start(db, 'second-turn') is not None
    assert selected() == [(second['id'], False)]


async def test_a_limit_held_head_moves_at_once_on_a_person_choice(accounts, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import infra.app_loop as app_loop

    db, first, second, _events = _configured(accounts, monkeypatch, enabled=False)
    _accept(db, 'input')
    assert type(_start(db, 'held')).__name__ == 'HeldAdmission'
    admitted = AsyncMock()
    monkeypatch.setattr('infra.thread_execution.drive_turn_admission', admitted)
    app_loop.capture(asyncio.get_running_loop())
    try:
        # The picker's own call: no runtime revision, as a person's choice carries none.
        moved = rebind_idle_account('destination', second['id'], user_id='user', db=db)
        for _ in range(500):
            if admitted.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()

    # Nothing is running, so nothing waits: the binding moves now and the held input claims on it.
    assert moved == {'account_id': second['id'], 'runtime_context_epoch': 1, 'changed': True}
    assert _bound(db) == (second['id'], None)
    batch = admitted.await_args.args[0]
    assert [item.message_id for item in batch.items] == ['input']
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?", (batch.turn_id,)).fetchone()[0] == second['id']
