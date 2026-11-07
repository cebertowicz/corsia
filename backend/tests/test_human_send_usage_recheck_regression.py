"""A fresh human WebSocket send must request one actual usage read on a held account.

Only the provider control-read boundary is mocked. The handler, durable FIFO,
account admission and hold projection remain real in the disposable database.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts, default_receipt, set_legacy_policy  # noqa: F401


class CaptureSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


@pytest.fixture
def limited_thread(accounts):
    from connections import harness_usage
    from connections.account_policy import select_account
    from infra.input_admission import accept_thread_input

    db, account, _, _ = accounts
    set_legacy_policy(
        'destination-project', 'codex', allowlist=[account['id']],
        default_account_id=account['id'], switch_on_limit=False, db=db,
    )
    db.execute("UPDATE models SET context_window=262144 WHERE id='codex/test'")
    db.commit()
    select_account('destination', 'codex', db=db)
    receipt = default_receipt('codex', db=db)
    harness_usage.record_limited(
        provider='codex', receipt=receipt, reason='usage_limit',
        resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update', db=db,
    )
    accept_thread_input(
        'destination', role='user', content='older held message', intent='apply_now',
        message_id='older-held', created_at='2026-11-06T19:00:00+00:00', db=db,
    )
    db.commit()
    from infra.held_admission import limited_admission
    assert limited_admission('destination', 'codex/test', db=db) is not None
    return db, account, receipt


@pytest.mark.asyncio
async def test_fresh_human_send_requests_one_control_read_and_retains_limited_fifo(limited_thread, monkeypatch):
    import ws
    from connections import harness_usage_check
    from infra.held_admission import limited_admission

    db, account, receipt = limited_thread
    control_read = AsyncMock(return_value=True)
    monkeypatch.setattr(harness_usage_check, '_check_codex', control_read)
    # Existing refresh throttling must not suppress this explicit human request.
    monkeypatch.setitem(harness_usage_check._LAST_STARTED, account['id'], __import__('time').monotonic())
    socket = CaptureSocket()

    await ws._handle_message(socket, {'id': 'user'}, 'user', {
        'thread_id': 'destination', 'input_id': 'new-human', 'content': 'try now',
    })
    # Join work the handler scheduled without invoking a checker ourselves.
    scheduled = tuple(harness_usage_check._CHECKS.values())
    if scheduled:
        await asyncio.gather(*scheduled)
    rows = db.execute(
        "SELECT id,delivery_state,active_turn_id,delivery_error FROM messages "
        "WHERE id IN ('older-held','new-human') ORDER BY input_order",
    ).fetchall()
    assert [row['id'] for row in rows] == ['older-held', 'new-human']
    assert all(row['delivery_state'] == 'queued' and row['active_turn_id'] is None for row in rows)
    assert limited_admission('destination', 'codex/test', db=db) is not None
    control_read.assert_awaited_once_with('codex', receipt)


@pytest.mark.asyncio
async def test_canonical_explicit_check_reaches_control_read_despite_recent_schedule(limited_thread, monkeypatch):
    from connections import harness_usage_check

    _, account, receipt = limited_thread
    control_read = AsyncMock(return_value=True)
    monkeypatch.setattr(harness_usage_check, '_check_codex', control_read)
    monkeypatch.setitem(harness_usage_check._LAST_STARTED, account['id'], __import__('time').monotonic())

    assert await harness_usage_check.check_login('codex', receipt) == 'pass'
    control_read.assert_awaited_once_with('codex', receipt)
