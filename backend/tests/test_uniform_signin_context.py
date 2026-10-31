"""Per-account discovery retains exact model context when a probe is skipped."""
import json
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts  # noqa: F401
from test_added_account_catalog import _discover, _row


@pytest.mark.asyncio
async def test_claude_skipped_probes_preserve_smaller_account_window(accounts, monkeypatch):
    from connections import claude_discovery
    from connections.harness_model_rows import normalize_claude_models
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, get_account, set_account_state

    db = accounts[0]
    upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/test'})
    small = add_account('claude-code', 'Small', db=db)
    small = set_account_state(small['id'], 'signed_in', expected_revision=small['revision'], db=db)
    large = add_account('claude-code', 'Large', db=db)
    large = set_account_state(large['id'], 'signed_in', expected_revision=large['revision'], db=db)
    calls = []
    async def control(_binary, _frames, **_kwargs):
        return json.dumps({'type': 'control_response', 'response': {'request_id': 'corsia-model-list',
            'subtype': 'success', 'response': {'models': [{'value': 'sonnet', 'resolvedModel': 'exact'}]}}}).encode()
    async def probe(_binary, selector, **_kwargs):
        calls.append(selector)
        return {'rawMaxTokens': 100000, 'maxTokens': 100000}
    monkeypatch.setattr(claude_discovery, 'run_claude_control', control)
    monkeypatch.setattr(claude_discovery, '_discover_claude_context', probe)
    for i in range(3):
        current = get_account(small['id'], db=db)
        staged = await claude_discovery.discover_claude_models('/synthetic/claude', context=SimpleNamespace(prior_discovery=current['discovery']))
        rows = normalize_claude_models(staged.payload, harness_version='1', timestamp='t')
        assert _discover(db, 'claude-code', current, rows).status == 'ready'
        if i == 0:
            large_row = _row('sonnet') | {'resolved_identity': 'exact', 'context_window': 200000}
            assert _discover(db, 'claude-code', large, [large_row]).status == 'ready'
        record = get_account(small['id'], db=db)['discovery']['discovery']['models']['claude-code/sonnet']
        assert record['context_window'] == 100000
        assert db.execute("SELECT context_window FROM models WHERE id='claude-code/sonnet'").fetchone()[0] == 100000
    assert calls == ['sonnet']


def test_changed_resolved_identity_drops_old_account_window(accounts):
    from connections.vendor_accounts import get_account

    db, _, account, _ = accounts
    assert _discover(db, 'codex', account, [_row('changed') | {'resolved_identity': 'old'}]).status == 'ready'
    assert _discover(db, 'codex', account, [_row('changed') | {'resolved_identity': 'new', 'context_window': None}]).status == 'ready'
    record = get_account(account['id'], db=db)['discovery']['discovery']['models']['codex/changed']
    assert record.get('context_window') is None
    assert db.execute("SELECT context_window FROM models WHERE id='codex/changed'").fetchone()[0] is None
