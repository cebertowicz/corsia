"""A missing account home holds the FIFO head through drive, settlement and recovery."""
from __future__ import annotations

import shutil

import pytest

from vendor_account_fixtures import accounts, vendors  # noqa: F401


@pytest.mark.asyncio
async def test_missing_home_stays_held_through_drive_and_recovery(vendors, monkeypatch, tmp_path):  # noqa: F811
    from connections.account_evidence import mark_discovery
    from connections.account_policy import AccountAdmissionError, select_account
    from connections.account_receipts import capture_receipt
    from infra import thread_execution, unavailable_model_recovery
    from infra.input_admission import accept_thread_input
    from infra.thread_driver import finish_turn
    from infra.thread_input_types import ClaimedBatch

    db, accounts = vendors
    account = accounts['claude-code']
    model = 'claude-code/test'
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES (?,?,?,'claude-code','test','claude-code','t','t')", (model, 'Test', model))
    db.execute("UPDATE threads SET model=? WHERE id='destination'", (model,))
    db.execute("UPDATE projects SET path=? WHERE id='destination-project'", (str(tmp_path),))
    receipt = capture_receipt('claude-code', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={model: {'available': True}}, db=db)
    db.commit()
    select_account('destination', 'claude-code', account_id=account['id'], db=db)
    shutil.rmtree(account['source_home'])
    accept_thread_input('destination', role='user', content='hello', intent='apply_now', message_id='input', db=db)
    db.commit()

    attempts = []
    original_drive = thread_execution.drive_claimed_batch
    claims = []

    async def bounded_drive(*args, **kwargs):
        claims.append(1)
        if len(claims) > 2:
            raise AssertionError('reclaim retry guard exceeded')
        return await original_drive(*args, **kwargs)

    async def refuse(*args, **kwargs):
        attempts.append(1)
        if len(attempts) > 2:
            raise AssertionError('launch retry guard exceeded')
        raise AccountAdmissionError('account_home_mismatch')

    monkeypatch.setattr('subagents.cc_runtime_helpers._main_working_dir', lambda *_: str(tmp_path))
    monkeypatch.setattr(thread_execution, 'drive_claimed_batch', bounded_drive)
    monkeypatch.setattr(thread_execution, '_dispatch_prepared', refuse)
    await thread_execution.drive_thread('destination')
    # Held by readiness before claim: no claim and no launch, not one claim refused at launch.
    assert (claims, attempts) == ([], [])
    row = db.execute("SELECT delivery_state,delivery_error,active_turn_id,write_outcome FROM messages WHERE id='input'").fetchone()
    assert tuple(row) == ('queued', 'account_home_mismatch', None, 'proved_no_write')

    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES ('current','destination',0,?,'running','t','boot')", (model,))
    db.commit()
    settlement = finish_turn('current', outcome='completed', ended_at='later', successor_turn_id='next',
                             boot_epoch='boot', db=db)
    assert not isinstance(settlement.successor, ClaimedBatch)
    assert db.execute("SELECT active_turn_id FROM messages WHERE id='input'").fetchone()[0] is None
    assert 'destination' in unavailable_model_recovery._held_thread_ids()
    await unavailable_model_recovery._resume_held_heads()
    assert (claims, attempts) == ([], [])
    row = db.execute("SELECT delivery_state,delivery_error,active_turn_id,write_outcome FROM messages WHERE id='input'").fetchone()
    assert tuple(row) == ('queued', 'account_home_mismatch', None, 'proved_no_write')
