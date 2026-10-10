"""Debug-model enrichment cannot overwrite staged discovery's account context."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_vendor_account_policy import accounts
from tests.test_vendor_account_claim_readiness import _ready


@pytest.mark.parametrize('inventory', ['matching', 'unmatched', 'empty'])
@pytest.mark.asyncio
async def test_actual_adapter_keeps_projection_retirement_identity(accounts, monkeypatch, inventory):
    from connections import codex_discovery
    from connections.account_discovery_types import capture_discovery_context, StagedDiscovery
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    _ready(db, custom)
    context = capture_discovery_context('codex', account_id=custom['id'], db=db)
    source = Path(context.source_home)
    assert source.is_dir()  # Account creation already owns the source directory.
    (source / 'auth.json').write_text('synthetic-noncredential-fixture')
    created = []
    class SyntheticSession:
        def __init__(self, **kwargs):
            self.home = Path(kwargs['codex_home'])
            assert kwargs['source_codex_home'] == context.source_home
            assert kwargs['control_only'] is True
            created.append(self)
        async def start(self):
            assert self.vendor_account_receipt == context.receipt
            assert (self.home / 'auth.json').is_symlink()
            (self.home / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'Own'}]}))
        async def list_models(self):
            return {'data': [] if inventory == 'empty' else [{'id': 'Own'}]}
        async def _request(self, method, args):
            assert method == 'account/rateLimits/read' and args is None
            return {'ordinaryUsageAllowed': True}
        def build_env(self):
            return {'CODEX_HOME': str(self.home)}
        async def close(self, **kwargs):
            self.closed = True
    async def debug(binary, *, env):
        assert binary == '/synthetic/codex'
        assert env == {'CODEX_HOME': str(created[0].home)}
        return {'models': [{'slug': 'Own' if inventory == 'matching' else 'Other', 'metadata': 'synthetic'}]}
    monkeypatch.setattr(codex_discovery, 'CodexAppServerSession', SyntheticSession)
    monkeypatch.setattr(codex_discovery, 'discover_codex_debug_models', debug)
    staged = await codex_discovery.discover_codex_models('/synthetic/codex', context=context)
    assert isinstance(staged, StagedDiscovery)
    assert created[0].closed
    home = created[0].home
    assert home.is_dir()
    assert not Path(context.runtime_home, 'models_cache.json').exists()
    before = get_account(custom['id'], db=db)['discovery']['auth_projection']['homes']
    assert before[str(home / 'auth.json')]['login_seq'] == context.receipt.login_seq
    if inventory == 'matching':
        assert staged.payload['data'][0]['contextMetadata']['slug'] == 'Own'
    elif inventory == 'unmatched':
        assert 'contextMetadata' not in staged.payload['data'][0]
    else:
        assert staged.payload['data'] == []
    staged.close()
    assert not home.exists()
    after = get_account(custom['id'], db=db)['discovery']['auth_projection']['homes']
    assert str(home / 'auth.json') not in after
    staged.close()


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.asyncio
async def test_account_projection_writes_stay_off_the_event_loop(accounts, monkeypatch, fail):
    """Gate, establish, validate and retire/retired run on the DB pool, never the loop thread."""
    from connections import codex_discovery
    from connections.account_discovery_refresh import refresh_account_once
    from connections.account_discovery_types import capture_discovery_context
    from connections.account_projection import validate_projection
    from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV

    db, _, custom, _ = accounts
    _ready(db, custom)
    context = capture_discovery_context('codex', account_id=custom['id'], db=db)
    (Path(context.source_home) / 'auth.json').write_text('synthetic-noncredential-fixture')
    homes = []

    class SyntheticSession:
        def __init__(self, **kwargs):
            self.home = Path(kwargs['codex_home'])
            homes.append(self.home)
        async def start(self):
            (self.home / 'models_cache.json').write_text(json.dumps({'models': []}))
        async def list_models(self):
            if fail:
                raise RuntimeError('synthetic discovery failure')
            return {'data': []}
        async def _request(self, method, args):
            return {}
        def build_env(self):
            validate_projection(self.vendor_account_receipt, home=self.home)
            return {}
        async def close(self, **kwargs):
            pass

    async def debug(binary, *, env):
        return {'models': []}

    async def db_call(fn, *args, **kwargs):
        return None

    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, '1')
    monkeypatch.setattr(codex_discovery, 'CodexAppServerSession', SyntheticSession)
    monkeypatch.setattr(codex_discovery, 'discover_codex_debug_models', debug)
    if fail:
        with pytest.raises(RuntimeError, match='synthetic discovery failure'):
            await codex_discovery.discover_codex_models('/synthetic/codex', context=context)
    else:
        async def version_read(binary, env=None):
            return '0'
        await refresh_account_once(
            'codex', context=context, binary='/synthetic/codex', harness_version='0',
            adapter=(codex_discovery.discover_codex_models, lambda payload, **kw: [], None),
            db_call=db_call, version_read=version_read, db=db)
    assert homes and not homes[0].exists()
