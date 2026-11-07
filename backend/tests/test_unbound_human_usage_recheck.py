"""First-use human ingress retains canonical policy selection before any thread binding."""
from __future__ import annotations

import asyncio

import pytest

from human_usage_fixtures import (  # noqa: F401
    CaptureSocket, accounts, notification_owner, unbound_held_subject, vendor_io,
    drain_background, human_send,
)

pytestmark = pytest.mark.real_harness_cli


class RecheckTrace:
    def __init__(self):
        from connections import provider_policy
        from infra import held_usage_recheck

        self.open_db = held_usage_recheck.get_db
        self.read_receipt = held_usage_recheck._limited_receipt
        self.select_rows = provider_policy.list_account_selection_facts
        self.schedule = held_usage_recheck.schedule_login_check
        self.connections = []
        self.queries = []
        self.selection_rows = []
        self.requests = []

    def open(self):
        db = self.open_db()
        self.connections.append(db)
        db.set_trace_callback(self.queries.append)
        return db

    def receipt(self, held):
        try:
            return self.read_receipt(held)
        finally:
            for db in self.connections:
                db.set_trace_callback(None)
            self.connections.clear()

    def selection(self, provider, *, db):
        rows = self.select_rows(provider, db=db)
        if db in self.connections:
            self.selection_rows.append([row['id'] for row in rows])
        return rows

    def request(self, provider, receipt):
        self.requests.append((provider, receipt))
        return self.schedule(provider, receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('worker', [False, True])
@pytest.mark.parametrize('verdict', ['limited', 'allowed'])
async def test_unbound_human_reads_policy_account_and_preserves_fifo(
    unbound_held_subject, vendor_io, notification_owner, monkeypatch, worker, verdict,
):
    from connections import provider_policy
    from infra import held_usage_recheck

    db, provider, model, account, receipt = unbound_held_subject
    assert db.execute('SELECT 1 FROM thread_vendor_accounts WHERE thread_id=?', ('destination',)).fetchone() is None
    if worker:
        db.execute("UPDATE threads SET spawned_by='actor' WHERE id='destination'")
        db.commit()
    trace = RecheckTrace()
    monkeypatch.setattr(held_usage_recheck, 'get_db', trace.open)
    monkeypatch.setattr(held_usage_recheck, '_limited_receipt', trace.receipt)
    monkeypatch.setattr(held_usage_recheck, 'schedule_login_check', trace.request)
    monkeypatch.setattr(provider_policy, 'list_account_selection_facts', trace.selection)
    vendor_io.verdict = verdict
    socket = CaptureSocket()
    await asyncio.wait_for(human_send(socket, worker=worker), 3)
    await asyncio.wait_for(vendor_io.started.wait(), 3)
    assert [event['input_id'] for event in socket.sent if event['type'] == 'input_queued'] == ['fresh']
    assert trace.requests == [(provider, receipt)]
    assert len(trace.selection_rows) == 1
    assert account['id'] in trace.selection_rows[0]
    assert db.execute('SELECT 1 FROM thread_vendor_accounts WHERE thread_id=?', ('destination',)).fetchone() is None
    assert [row[0] for row in db.execute(
        "SELECT id FROM messages WHERE thread_id='destination' AND role='user' AND delivery_state='queued' ORDER BY input_order",
    )] == ['older', 'fresh']
    selects = [query for query in trace.queries if query.lstrip().upper().startswith('SELECT')]
    assert selects and not any('messages' in query.lower() for query in selects)
    assert trace.queries[0] == 'BEGIN'
    print(f'{provider}/{worker}/{verdict}: added-path SELECTs={len(selects)}; '
          f'canonical selection enumerations=1; account-selection rows={len(trace.selection_rows[0])}; history reads=0')
    vendor_io.finish.set()
    await drain_background()
    assert len(vendor_io.reads) == 1
    if verdict == 'allowed':
        assert len(notification_owner) == 1
        batch = notification_owner[0]
        assert batch.vendor_receipt == receipt
        assert [item.message_id for item in batch.items if not item.message_id.startswith('context-digest:')] == ['older', 'fresh']
    else:
        assert notification_owner == []
    assert [row[0] for row in db.execute(
        "SELECT delivery_state FROM messages WHERE id IN ('older','fresh') ORDER BY input_order",
    )] == (['delivered', 'delivered'] if verdict == 'allowed' else ['queued', 'queued'])
