"""Human ingress reaches real account control orchestration; only vendor I/O is synthetic."""
from __future__ import annotations

import asyncio

import pytest

from human_usage_fixtures import (  # noqa: F401
    CaptureSocket, accounts, notification_owner, held_subject, vendor_io,
    drain_background, human_send,
)


pytestmark = pytest.mark.real_harness_cli  # Real orchestration over synthetic subprocess/session I/O.


def queued_ids(db):
    return [row[0] for row in db.execute(
        "SELECT id FROM messages WHERE thread_id='destination' AND role='user' AND delivery_state='queued' ORDER BY input_order",
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize('worker', [False, True])
@pytest.mark.parametrize('verdict', ['limited', 'error', 'unknown'])
async def test_real_controls_retain_fifo_and_ack_before_blocked_io(
    held_subject, vendor_io, notification_owner, worker, verdict,
):
    db, provider, model, account, receipt = held_subject
    if worker:
        db.execute("UPDATE threads SET spawned_by='actor' WHERE id='destination'")
        db.commit()
    vendor_io.verdict = verdict
    socket = CaptureSocket()
    await asyncio.wait_for(human_send(socket, worker=worker), 3)
    await asyncio.wait_for(vendor_io.started.wait(), 3)

    assert not vendor_io.finish.is_set()
    assert [event['input_id'] for event in socket.sent if event['type'] == 'input_queued'] == ['fresh']
    assert queued_ids(db) == ['older', 'fresh']
    vendor_io.finish.set()
    await drain_background()
    assert len(vendor_io.reads) == 1
    assert vendor_io.reads[0][0] == provider
    if provider == 'codex':
        assert vendor_io.reads[0][1] == receipt
        assert vendor_io.sessions[0].closed
    assert queued_ids(db) == ['older', 'fresh']
    assert notification_owner == []
    from infra.held_admission import limited_admission
    assert limited_admission('destination', model, db=db) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('worker', [False, True])
async def test_allowed_read_releases_existing_fifo_once(held_subject, vendor_io, notification_owner, worker):
    from connections import harness_usage
    from infra.state.ws import notify_models_changed
    from infra.thread_driver import finish_turn

    db, provider, model, account, receipt = held_subject
    if worker:
        db.execute("UPDATE threads SET spawned_by='actor' WHERE id='destination'")
        db.commit()
    vendor_io.verdict = 'allowed'
    socket = CaptureSocket()
    await human_send(socket, worker=worker)
    await asyncio.wait_for(vendor_io.started.wait(), 3)
    expected_prefix = [row[0] for row in db.execute(
        "SELECT id FROM messages WHERE thread_id='destination' AND delivery_state='queued' ORDER BY input_order",
    )]
    vendor_io.finish.set()
    await drain_background()
    assert len(vendor_io.reads) == 1
    assert harness_usage.active_limit(provider=provider, account_id=account['id'], db=db) is None
    assert len(notification_owner) == 1, [dict(row) for row in db.execute(
        "SELECT id,delivery_state,delivery_error,active_turn_id FROM messages WHERE thread_id='destination'",
    )]
    batch = notification_owner[0]
    assert [item.message_id for item in batch.items] == expected_prefix
    assert [identity for identity in expected_prefix if not identity.startswith('context-digest:')] == ['older', 'fresh']
    assert batch.vendor_receipt == receipt
    assert queued_ids(db) == []
    finish_turn(batch.turn_id, outcome='completed', ended_at='done', successor_turn_id='successor',
                boot_epoch='test', db=db)
    notify_models_changed()
    await drain_background()
    assert len(notification_owner) == 1
    assert [row[0] for row in db.execute(
        "SELECT delivery_state FROM messages WHERE id IN ('older','fresh') ORDER BY input_order",
    )] == ['delivered', 'delivered']


@pytest.mark.asyncio
async def test_fresh_concurrent_sends_coalesce_and_duplicates_do_not_recheck(
    held_subject, vendor_io, notification_owner,
):
    db, *_ = held_subject
    socket = CaptureSocket()
    await human_send(socket)
    await asyncio.wait_for(vendor_io.started.wait(), 3)
    await asyncio.gather(human_send(socket, input_id='second'), human_send(socket, input_id='third'))
    assert len(vendor_io.reads) == 1
    vendor_io.finish.set()
    await drain_background()
    await human_send(socket)
    await drain_background()
    assert len(vendor_io.reads) == 1
    assert queued_ids(db)[:2] == ['older', 'fresh']
    assert set(queued_ids(db)) == {'older', 'fresh', 'second', 'third'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['delegation', 'automation', 'ws'])
async def test_machine_role_or_metadata_cannot_claim_human_provenance(
    held_subject, vendor_io, notification_owner, mode,
):
    from infra.send_service import send_thread_input

    db, *_ = held_subject
    row = db.execute("SELECT * FROM threads WHERE id='destination'").fetchone()
    await send_thread_input('destination', row, 'machine wake', role='user', native_mode=mode,
                            ws=CaptureSocket() if mode == 'ws' else None,
                            input_id='machine', metadata={'human_origin': True, 'source': 'human'})
    await drain_background()
    assert vendor_io.reads == []
    assert queued_ids(db) == ['older', 'machine']


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['signed_out', 'new_login'])
async def test_read_cannot_release_replaced_login(held_subject, vendor_io, notification_owner, replacement):
    from connections import harness_usage
    from connections.account_evidence import invalidate_discovery, mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account

    db, provider, model, account, receipt = held_subject
    vendor_io.verdict = 'allowed'
    await human_send(CaptureSocket())
    await asyncio.wait_for(vendor_io.started.wait(), 3)
    current = get_account(account['id'], db=db)
    if replacement == 'signed_out':
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (account['id'],))
        db.commit()
    else:
        invalidate_discovery(current, login_seq=receipt.login_seq + 1, auth_state='known', db=db)
        new = capture_receipt(provider, account_id=account['id'], db=db)
        assert new != receipt
        mark_discovery(account['id'], receipt=new, status='ready', models={model: {'available': True}}, db=db)
        db.commit()
        harness_usage.record_limited(provider=provider, receipt=new, reason='new-login-limit',
                                     resets_at=None, source='test', db=db)
        db.commit()
    vendor_io.finish.set()
    await drain_background()
    assert len(vendor_io.reads) == 1
    assert queued_ids(db) == ['older', 'fresh']
    assert notification_owner == []
    if replacement == 'new_login':
        assert harness_usage.active_limit(provider=provider, account_id=account['id'], db=db)['reason'] == 'new-login-limit'


@pytest.mark.asyncio
async def test_unauthorized_and_malformed_inputs_never_read(held_subject, vendor_io, notification_owner):
    db, *_ = held_subject
    await human_send(CaptureSocket(), user='different-user')
    await human_send(CaptureSocket(), input_id='')
    await drain_background()
    assert vendor_io.reads == []
    assert queued_ids(db) == ['older']
