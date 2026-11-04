"""Successors of pre-write Claude Code refusals run outside the failing feed."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import fake_claude  # noqa: F401
from vendor_account_fixtures import _accept, _ready, _row, _start, accounts  # noqa: F401


@pytest.mark.asyncio
async def test_consecutive_prewrite_refusals_drive_each_successor_on_its_own_task(monkeypatch):
    from core.turn_core import TerminalResult, TerminalSettlement
    from subagents import cc_feed
    from subagents.cc_process_exit import _CommittedTerminalFailure

    count = 30
    tasks = []
    finished = asyncio.Event()
    session = SimpleNamespace(_terminal_driver=None, _proc=None, corsia_thread_id='thread')
    live = SimpleNamespace(session=session)
    lock = asyncio.Lock()
    registry = SimpleNamespace(get=lambda _: live, lock=lambda _: lock)
    transition = SimpleNamespace(
        thread_id='thread', held_lock=lock, require_idle=AsyncMock(return_value=live),
        require_direct_fit=lambda: None, replace_quarantined=AsyncMock(return_value=live),
        refresh_once=AsyncMock(), consume_cold_transition=AsyncMock(return_value=live),
    )
    target = SimpleNamespace(harness='claude-code', transport_model='claude-code/test')

    async def refuse(*_):
        tasks.append(asyncio.current_task())
        raise _CommittedTerminalFailure(
            TerminalSettlement(TerminalResult.APPLIED, 'thread'), RuntimeError('refused')
        )

    async def drive(_):
        if len(tasks) < count:
            with pytest.raises(RuntimeError, match='refused'):
                await cc_feed.ensure_live_then_feed('thread', 'hello', target=target, turn_id='turn')
        else:
            finished.set()

    monkeypatch.setattr(cc_feed, '_LIVE', registry)
    monkeypatch.setattr(cc_feed, 'CCFeedTransition', lambda **_: transition)
    monkeypatch.setattr(cc_feed, 'require_owned_accept_db', lambda _: None)
    monkeypatch.setattr(cc_feed, '_settle_terminal_boundary', AsyncMock())
    monkeypatch.setattr(cc_feed, 'replace_if_required', AsyncMock(return_value=live))
    monkeypatch.setattr(cc_feed, '_accept_confirming_resume', refuse)
    monkeypatch.setattr('infra.thread_execution.drive_terminal_settlement', drive)

    with pytest.raises(RuntimeError, match='refused'):
        await asyncio.wait_for(
            cc_feed.ensure_live_then_feed('thread', 'hello', target=target, turn_id='turn'), timeout=5
        )
    await asyncio.wait_for(finished.wait(), timeout=5)
    assert len(tasks) == count
    assert len(set(tasks)) == count


@pytest.mark.asyncio
async def test_resume_failure_drives_settlement_after_feed_returns(monkeypatch):
    from core.turn_core import TerminalResult, TerminalSettlement
    from subagents import cc_feed
    from subagents.cc_process_exit import _CommittedTerminalFailure

    order = []
    finished = asyncio.Event()
    lock = asyncio.Lock()
    settlement = TerminalSettlement(TerminalResult.APPLIED, 'thread')

    async def refuse():
        raise _CommittedTerminalFailure(settlement, RuntimeError('launch refused'))

    async def drive(actual):
        assert actual is settlement
        order.append('driver')
        finished.set()

    transition = SimpleNamespace(thread_id='thread', held_lock=lock, resume=refuse)
    monkeypatch.setattr(cc_feed, '_LIVE', SimpleNamespace(get=lambda _: None, lock=lambda _: lock))
    monkeypatch.setattr(cc_feed, 'CCFeedTransition', lambda **_: transition)
    monkeypatch.setattr(cc_feed, 'require_owned_accept_db', lambda _: None)
    monkeypatch.setattr('infra.thread_execution.drive_terminal_settlement', drive)
    target = SimpleNamespace(harness='claude-code', transport_model='claude-code/test')

    with pytest.raises(RuntimeError, match='launch refused'):
        await cc_feed.ensure_live_then_feed('thread', 'hello', target=target, turn_id='turn')
    order.append('caller')
    await asyncio.wait_for(finished.wait(), timeout=5)
    assert order == ['caller', 'driver']


def test_codex_refusal_holds_until_new_sign_in(accounts, monkeypatch):  # noqa: F811
    from pathlib import Path
    from connections.account_holds import refresh_account_holds
    from connections.account_lifecycle import change_state
    from connections.account_policy import AccountAdmissionError, require_claim, require_launch, select_account
    from connections.account_projection import cleanup_stale, ensure_projection
    from connections.account_receipts import capture_receipt
    from connections.account_runtime import homes_for
    from connections.vendor_accounts import get_account
    from infra.database import write_transaction
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import finish_turn

    db, _, account, _ = accounts
    select_account('destination', 'codex', account_id=account['id'], db=db)
    receipt = _ready(db, account)
    ensure_projection(receipt, home=Path(account['runtime_home']), kind='live', db=db)
    auth = Path(account['source_home'], 'auth.json')
    valid_auth = auth.read_bytes()
    auth.write_text('{invalid')
    assert require_claim('codex', thread_id='destination', model='codex/test', db=db) == account['id']
    _accept(db, 'm')
    batch = _start(db, 'first')
    begin_batch_write(batch, attempt_id='attempt-first', started_at='t', db=db)
    with pytest.raises(AccountAdmissionError) as refusal:
        require_launch('codex', thread_id='destination', account_id=account['id'], turn_id='first',
                       frozen=True, model='codex/test', launch=homes_for(account), receipt=receipt, db=db)
    assert refusal.value.reason == 'account_login_unverified'
    settle_batch_write('attempt-first', outcome='proved_no_write', error=refusal.value, db=db)
    result = finish_turn('first', outcome='error', error=refusal.value, ended_at='t',
                         successor_turn_id='successor', boot_epoch='test', db=db)
    assert result.successor is None
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='destination'").fetchone()[0] == 1
    held = _row(db)
    assert (held['delivery_state'], held['write_outcome'], held['delivery_error']) == (
        'queued', 'proved_no_write', 'account_login_unverified')
    assert held['active_turn_id'] is None

    wakes = []
    monkeypatch.setattr('infra.state.ws.schedule_models_changed', lambda: wakes.append('wake'))
    auth.write_bytes(valid_auth)
    current = get_account(account['id'], db=db)
    change_state(account['id'], 'signed_in', expected_revision=current['revision'], db=db)
    fresh = capture_receipt('codex', account_id=account['id'], db=db)
    assert cleanup_stale(fresh, quiesced=True, db=db)
    _ready(db, account)
    from infra.unavailable_model_recovery import held_head_kind_sql
    from connections.account_holds import model_account_hold
    assert db.execute(f"SELECT {held_head_kind_sql(':thread')} AS kind", {'thread': 'destination'}).fetchone()['kind'] == 'account'
    assert model_account_hold('destination', 'codex/test', db=db) is None
    with write_transaction(db, scope='test_new_sign_in'):
        refresh_account_holds(account['id'], db=db)
    assert wakes
    retry = _start(db, 'retry')
    assert [item.message_id for item in retry.items] == ['m']
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='destination'").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_real_refusals_claim_and_drive_successors(
    isolated_db, signed_in_defaults, fake_claude, monkeypatch, tmp_path,
):
    import connections.models as models
    import subagents.vendor_write_admission as admission
    from connections.account_policy import AccountAdmissionError
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from infra.thread_execution import execute_claimed_batch
    from subagents.cc_runtime_state import _LIVE
    from cc_runtime_fixtures import cc_target

    calls = []
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / '.claude'))
    monkeypatch.setattr(models, 'get_connection', lambda provider, db=None: {'config': {'binary': fake_claude}})
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('refusals','user-1','claude-code/sonnet','t','t')")
    db.commit()
    first = admit_input('refusals', role='user', content='first', intent='apply_now',
                        message_id='input-0', turn_id='turn-0', boot_epoch=BOOT_EPOCH, admitted_at='t', db=db)
    assert first.batch is not None

    require_write = admission.require_session_write

    def refuse(_session, _provider):
        try:
            task = asyncio.current_task()
        except RuntimeError:  # launch admission runs in the DB worker
            return require_write(_session, _provider)
        calls.append(task)
        raise AccountAdmissionError('account_login_unverified')

    monkeypatch.setattr(admission, 'require_session_write', refuse)
    try:
        with pytest.raises(AccountAdmissionError):
            await execute_claimed_batch(first.batch, thread_row=dict(db.execute(
                "SELECT * FROM threads WHERE id='refusals'").fetchone()), target=cc_target('sonnet'))
        live = _LIVE.get('refusals')
        await asyncio.wait_for(live.session._terminal_driver, timeout=30)
        rows = db.execute("SELECT id, status FROM active_turns WHERE thread_id='refusals'").fetchall()
        assert len(calls) == 1
        assert len(rows) == 1
        held = db.execute("SELECT delivery_state,write_outcome,delivery_error FROM messages WHERE id='input-0'").fetchone()
        assert tuple(held) == ('queued', 'proved_no_write', 'account_login_unverified')
    finally:
        live = _LIVE.get('refusals')
        if live is not None and live.session is not None:
            await live.session.terminate()
        _LIVE.reset()
        db.close()
