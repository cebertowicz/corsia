"""The failed Codex launch publishes its account and readable recovery text."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra.queries import insert_message
from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
async def test_cache_error_marker_stores_plain_text_and_account(accounts):
    from subagents.codex_runtime_errors import cache_error_marker, CodexModelCacheChanged

    db, _, account, _ = accounts
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('turn','destination',0,'codex/test','running','t','test',?)", (account['id'],))
    db.commit()
    identity = SimpleNamespace(turn_id='turn')
    error = CodexModelCacheChanged()
    marker = cache_error_marker(error, 'destination', SimpleNamespace(corsia_turn=identity),
                                lambda *_: None, db, insert_message, logging.getLogger(__name__))
    await marker()
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='destination'").fetchone()
    metadata = json.loads(row['metadata'])
    assert row['content'] == f"Couldn’t start Codex on Second: its model list is out of date. Reference: {metadata['ref']}"
    assert metadata['account_id'] == account['id']
    assert metadata['ref'].startswith('local-')


@pytest.mark.asyncio
async def test_failed_spawn_keeps_code_and_persists_account_marker(accounts, monkeypatch):
    from subagents import codex_runtime
    from subagents.codex_runtime_errors import CodexModelCacheChanged

    db, _, account, _ = accounts
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('turn','destination',0,'codex/test','running','t','test',?)", (account['id'],))
    db.commit()
    identity = SimpleNamespace(turn_id='turn')
    live = SimpleNamespace(corsia_turn=identity, input_delivery_unproven=False, session=None)
    terminal = AsyncMock()
    async def end(*args, **kwargs):
        await kwargs['marker']()
        assert kwargs['error'] == 'account_model_cache_changed'
    monkeypatch.setattr(codex_runtime, '_end_owned_turn', end)
    monkeypatch.setattr(codex_runtime, '_discard_delivery_event_barrier', lambda live: None)
    monkeypatch.setattr(codex_runtime, '_release_exact_live', lambda *args: None)
    error = CodexModelCacheChanged()
    await codex_runtime._finish_failed_app_server_spawn(
        'destination', live, None, error, lambda *_: None, db=db,
        terminalize=True, settle_delivery_terminal=terminal,
    )
    terminal.assert_awaited_once_with('error', 'account_model_cache_changed')
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='destination'").fetchone()
    metadata = json.loads(row['metadata'])
    assert row['content'] == f"Couldn’t start Codex on Second: its model list is out of date. Reference: {metadata['ref']}"
    assert metadata['account_id'] == account['id'] and metadata['ref'].startswith('local-')


@pytest.mark.asyncio
async def test_app_server_propagates_original_cache_error(accounts, monkeypatch, tmp_path):
    from subagents import codex_env, codex_runtime
    from subagents.codex_runtime_errors import CodexModelCacheChanged

    db, _, account, _ = accounts
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('turn','destination',0,'codex/test','running','t','test',?)", (account['id'],))
    db.commit()
    identity = SimpleNamespace(turn_id='turn')
    live = SimpleNamespace(corsia_turn=identity, input_delivery_unproven=False, session=None,
                           checkpoint_wait_scope=SimpleNamespace(stop_requested=False))
    raised = []
    terminal = AsyncMock()
    spec = SimpleNamespace(db=db, broadcast=lambda *_: None, thread_id='destination', working_dir=None,
        tool_registry=None, dynamic_tools=None, model='codex/test', codex_thread_id=None,
        catalog_model_id=None, runtime_context_epoch=0, windows=None, expected_reported_model=None,
        context_snapshot=None, harness='codex', continuity_accounting=None, continuing_wait_scope=None,
        prepared_context=None, held_lock=None, text='hello', successor_turn_id='turn', preadmitted_turn=None,
        transfer_delivery_to_turn=None, settle_delivery_terminal=terminal)
    monkeypatch.setattr(codex_runtime, '_require_owned_accept_db', lambda db: None)
    monkeypatch.setattr(codex_runtime, '_reusable_app_session', lambda _: None)
    monkeypatch.setattr(codex_runtime, '_resolve_app_server_tools', lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(codex_runtime, '_validate_codex_transport_model', lambda model: model)
    monkeypatch.setattr(codex_runtime, '_new_app_server_live', lambda *args, **kwargs: live)
    monkeypatch.setattr(codex_runtime, '_make_on_event', lambda *args: None)
    monkeypatch.setattr(codex_runtime, '_checkpoint_replay_id', lambda _: None)
    monkeypatch.setattr(codex_runtime, '_LIVE', SimpleNamespace(publish=lambda *args, **kwargs: None))
    monkeypatch.setattr(codex_runtime, '_start_live_turn', AsyncMock(return_value='turn'))
    async def configure(*args, **kwargs):
        try:
            codex_env._write_governed_model_catalog(
                source=tmp_path, destination=tmp_path, catalog_source=tmp_path / 'missing', cache_receipt=None,
            )
        except CodexModelCacheChanged as exc:
            raised.append(exc)
            raise
    monkeypatch.setattr(codex_runtime, '_configure_spec_app_server_input', configure)
    monkeypatch.setattr(codex_runtime, '_discard_delivery_event_barrier', lambda live: None)
    monkeypatch.setattr(codex_runtime, '_release_exact_live', lambda *args: None)
    async def end(*args, **kwargs):
        assert kwargs['error'] == 'account_model_cache_changed'
        await kwargs['marker']()
    monkeypatch.setattr(codex_runtime, '_end_owned_turn', end)
    with pytest.raises(CodexModelCacheChanged) as caught:
        await codex_runtime._run_app_server_turn(spec)
    assert caught.value is raised[0]
    terminal.assert_awaited_once_with('error', 'account_model_cache_changed')
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='destination'").fetchone()
    ref = json.loads(row['metadata'])['ref']
    assert row['content'] == f'Couldn’t start Codex on Second: its model list is out of date. Reference: {ref}'
    assert ref.startswith('local-')
