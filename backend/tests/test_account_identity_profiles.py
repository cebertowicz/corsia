"""What one account's identity, profile and owned catalog look like to the rest of the app.

Pins: per-account profiles and projections, the account-scoped catalog API and its owned
rows, the version/home an account runs on, the per-account cache stage and its publication,
and that a refresh registry belongs to exactly one owning account."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from tempfile import mkdtemp
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from connections.model_cache_stage import ModelCacheStage, validate_cache
from vendor_account_fixtures import _ready, accounts, claude, ready  # noqa: F401


def _stage(target, slug):
    from connections.account_discovery_types import StagedDiscovery
    from connections.model_cache_stage import ModelCacheStage

    root = Path(mkdtemp(prefix='actual-bundle-', dir=target.parent))
    fresh = root / 'models_cache.json'
    fresh.write_text(json.dumps({'models': [{'slug': slug}]}))
    return StagedDiscovery({}, cache_stage=ModelCacheStage.prepare(fresh, target),
                           cleanup_owner=lambda: shutil.rmtree(root)), root


def _bundle(db, account, tmp_path):
    from connections.account_discovery_types import capture_discovery_context
    from connections.account_evidence import mark_discovery
    from connections.model_cache_stage import ModelCacheStage

    _ready(db, account)
    context = capture_discovery_context('codex', account_id=account['id'], db=db)
    target = Path(context.runtime_home) / 'models_cache.json'
    old = tmp_path / 'old'
    old.write_text(json.dumps({'models': [{'slug': 'Old'}]}))
    prior_receipt = ModelCacheStage.prepare(old, target).promote()
    assert mark_discovery(account['id'], receipt=context.receipt, status='ready',
        models={'codex/old': {'available': True}}, cache_receipt=prior_receipt, db=db)
    db.commit()
    stage, root = _stage(target, 'New')
    return context, stage, root, target


def _commit(db, context, stage):
    from connections.account_discovery_commit import commit_discovery_bundle

    return commit_discovery_bundle('codex', stage, [{'selector_id': 'new', 'available': True}],
                                   context=context, harness_version='synthetic', db=db)


def _cache_receipt(db, account_id):
    from connections.vendor_accounts import get_account

    return get_account(account_id, db=db)['discovery']['discovery']['cache_receipt']


def _governed(context, tmp_path, target, receipt):
    from subagents.codex_env import _write_governed_model_catalog

    destination = tmp_path / 'governed'
    destination.mkdir(exist_ok=True)
    output = _write_governed_model_catalog(source=Path(context.source_home), destination=destination,
                                           catalog_source=target, cache_receipt=receipt)
    return json.loads(output.read_text())['models'][0]['slug']


def _slug(target):
    return json.loads(target.read_text())['models'][0]['slug']


async def _refresh(monkeypatch, provider, *, is_default=False, source_home='/synthetic/./source'):
    from connections import harness_models
    from connections.account_discovery_types import StagedDiscovery

    context = SimpleNamespace(provider=provider, is_default=is_default, source_home=source_home,
                              prior_discovery=None)
    calls, writes = [], []
    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        class Process:
            returncode = 0
            async def communicate(self):
                return b'synthetic CLI 1.2.3', b''
        return Process()
    async def discover(binary, **kwargs):
        assert binary == '/synthetic/vendor' and kwargs == {'context': context}
        return StagedDiscovery({'models': []})
    async def db_call(function, *args, **kwargs):
        writes.append((function.__name__, kwargs))
        return kwargs.get('cause', 'committed')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr('subagents.codex_env.clean_codex_env', lambda: {'CLEAN': 'codex'})
    monkeypatch.setattr('subagents.cc_env.clean_claude_env', lambda: {'CLEAN': 'claude-code'})
    monkeypatch.setattr(harness_models, '_db_call', db_call)
    monkeypatch.setitem(harness_models._ADAPTERS, provider, (discover, lambda *args, **kwargs: [], 'synthetic'))
    result = await harness_models._refresh_provider_once(provider, reason='signin',
                binary='/synthetic/vendor', context=context)
    return result, calls, writes


@pytest.fixture
def client_refuses_unsigned_api(accounts):
    from api.provider_policy import router as provider_policy_router
    from api.vendor_accounts import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.include_router(provider_policy_router)
    with TestClient(app) as api:
        assert api.get("/api/vendor-accounts").status_code == 401
        app.dependency_overrides[require_user] = lambda: {"id": "user"}
        yield api


def _catalog(db, custom):
    from llm.account_catalog import project_accounts

    return project_accounts([{'id': 'codex/test', 'litellm_model': 'codex/test', 'connection': 'codex',
        'status': 'ready', 'context_window': 999, 'capabilities': {'foreign': True},
        'resolved_identity': 'Default'}], account_id=custom['id'], project_id='destination-project', db=db)[0]


def _identity_bytes(db, account_id):
    return db.execute("SELECT identity_json,updated_at,revision FROM vendor_accounts WHERE id=?",
                      (account_id,)).fetchone()[:]


@pytest.fixture
def client(accounts):
    from api.provider_policy import router as provider_policy_router
    from api.vendor_accounts import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.include_router(provider_policy_router)
    with TestClient(app) as api:
        app.dependency_overrides[require_user] = lambda: {"id": "user"}
        yield api


@pytest.fixture
def synthetic(accounts, monkeypatch, tmp_path):
    """A registered non-inference provider with its own connection, homes and hint validator."""
    from connections import account_profiles
    from connections.models import upsert_connection

    monkeypatch.setattr(account_profiles, "_PROFILES", dict(account_profiles._PROFILES))

    def validate(hint):
        return {"login": hint["login"]}

    async def sign_out(account, source, runtime):
        return {"status": "done", "cause": None}

    account_profiles.register(account_profiles.AccountProfile(
        provider="synthetic", label="Synthetic", home_variable="SYNTHETIC_HOME",
        home_root=lambda: tmp_path / "synthetic-homes",
        commands={"login": ("login",), "status": ("status",), "logout": ("logout",)},
        missing_cause="synthetic_missing", in_progress_cause="synthetic_signin_in_progress",
        identity_facts=("login", "plan"), comparison_keys=("login",), status_report_keys={},
        clean_env=lambda: {}, default_warm_home=lambda: str(tmp_path / "warm"),
        validate_hint=validate, workspace_default=True, sign_out=sign_out))
    upsert_connection(type="cli", provider="synthetic", status="available", config={})
    return account_profiles.profile_for("synthetic")


def _setup(accounts):
    from connections.account_receipts import capture_receipt

    db, _, custom, _ = accounts
    source = Path(custom['source_home'])
    source.mkdir(parents=True, exist_ok=True)
    (source / 'auth.json').touch()  # The projection owner never reads these bytes.
    return db, custom, capture_receipt('codex', account_id=custom['id'], db=db)


def _establish(db, custom, receipt, home=None, kind='live'):
    from connections.account_projection import ensure_projection

    home = Path(home or custom['runtime_home'])
    ensure_projection(receipt, home=home, kind=kind, owner_turn_id='T' if kind != 'live' else None, db=db)
    return home / 'auth.json'


def test_success_commits_receipt_then_publishes_own_cache(accounts, tmp_path):
    from connections.model_cache_stage import validate_cache
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    old_receipt = _cache_receipt(db, custom['id'])
    result = _commit(db, context, stage)
    stage.close()
    assert result.status == 'ready' and result.effective_receipt == context.receipt
    assert not root.exists()
    discovery = get_account(custom['id'], db=db)['discovery']['discovery']
    assert discovery['models'] == {'codex/new': {'selector_id': 'new', 'available': True}}
    assert validate_cache(discovery['cache_receipt'], target)
    assert _governed(context, tmp_path, target, discovery['cache_receipt']) == 'New'
    with pytest.raises(RuntimeError, match='account_model_cache_changed'):
        _governed(context, tmp_path, target, old_receipt)


def test_bundle_nested_in_another_writer_is_refused_and_changes_nothing(accounts, tmp_path):
    from connections.vendor_accounts import get_account
    from infra.database import TransactionOwnershipError, write_transaction

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    before = get_account(custom['id'], db=db)
    with pytest.raises(TransactionOwnershipError, match='outermost'):
        with write_transaction(db, scope='outer'):
            _commit(db, context, stage)
    assert _slug(target) == 'Old'
    assert get_account(custom['id'], db=db) == before


@pytest.mark.parametrize('failure', ['raise', 'sqlite_auto_abort'])
def test_failed_bundle_publishes_nothing_and_old_receipt_still_reads_old_cache(accounts, tmp_path, monkeypatch, failure):
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    before = get_account(custom['id'], db=db)
    db.execute('CREATE TABLE auto_abort_probe(value TEXT)')
    db.execute("CREATE TRIGGER rollback_probe BEFORE INSERT ON auto_abort_probe "
               "BEGIN SELECT RAISE(ROLLBACK, 'synthetic auto abort'); END")
    db.commit()
    notifications = []
    monkeypatch.setattr('infra.state.ws.schedule_models_changed', lambda: notifications.append('models'))

    def fail(account_id, *, db):
        if failure == 'raise':
            raise RuntimeError('bundle statement failed')
        db.execute("INSERT INTO auto_abort_probe VALUES ('abort')")
    monkeypatch.setattr('connections.account_holds.refresh_account_holds', fail)
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        _commit(db, context, stage)
    stage.close()
    assert not db.in_transaction and not root.exists()
    assert notifications == []
    assert _slug(target) == 'Old'
    assert get_account(custom['id'], db=db) == before
    assert _governed(context, tmp_path, target, _cache_receipt(db, custom['id'])) == 'Old'


def test_commit_refused_by_sqlite_publishes_nothing(accounts, tmp_path):
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    before = get_account(custom['id'], db=db)
    path = db.execute('PRAGMA database_list').fetchone()[2]
    # Force genuine SQLITE_BUSY at COMMIT in rollback-journal mode using a reader.
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('PRAGMA busy_timeout=0')
    reader = sqlite3.connect(path, timeout=0)
    reader.execute('BEGIN')
    reader.execute('SELECT id FROM vendor_accounts').fetchall()
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            _commit(db, context, stage)
    finally:
        reader.close()
    stage.close()
    assert _slug(target) == 'Old'
    assert get_account(custom['id'], db=db) == before
    assert not root.exists()


def test_superseded_publication_never_overwrites_the_successor_cache(accounts, tmp_path, monkeypatch):
    from connections import account_discovery_commit
    from connections.model_cache_stage import validate_cache

    db, _, custom, _ = accounts
    context, first, _, target = _bundle(db, custom, tmp_path)
    successor, _ = _stage(target, 'Successor')
    path = db.execute('PRAGMA database_list').fetchone()[2]
    publish = account_discovery_commit._publish_cache
    seen = []

    def successor_commits_first(account_id, stage, cache_receipt, connection_arg):
        if stage is first.cache_stage:
            contender = sqlite3.connect(path, timeout=5)
            contender.row_factory = sqlite3.Row
            try:
                assert _commit(contender, context, successor).status == 'ready'
            finally:
                contender.close()
            seen.append('successor published')
        publish(account_id, stage, cache_receipt, connection_arg)
    monkeypatch.setattr(account_discovery_commit, '_publish_cache', successor_commits_first)
    assert _commit(db, context, first).status == 'ready'
    assert seen == ['successor published']
    receipt = _cache_receipt(db, custom['id'])
    assert _slug(target) == 'Successor' and validate_cache(receipt, target)
    assert _governed(context, tmp_path, target, receipt) == 'Successor'
    assert first.cache_stage.fresh.exists(), 'the superseded stage was never renamed into place'


@pytest.mark.asyncio
async def test_unpublishable_cache_fails_closed_then_canonical_refresh_recovers(accounts, tmp_path, monkeypatch, caplog):
    from connections import harness_models

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    replacement = tmp_path / 'replacement'
    replacement.write_text(json.dumps({'models': [{'slug': 'Foreign'}]}))
    replacement.replace(target)
    with caplog.at_level(logging.ERROR, logger='infra.write_effects'):
        assert _commit(db, context, stage).status == 'ready'
    assert 'after-commit effect failed' in caplog.text
    assert _slug(target) == 'Foreign'
    with pytest.raises(RuntimeError, match='account_model_cache_changed'):
        _governed(context, tmp_path, target, _cache_receipt(db, custom['id']))

    async def synthetic_adapter(binary, *, context):
        staged, _ = _stage(target, 'Fresh')
        return staged
    monkeypatch.setitem(harness_models._ADAPTERS, 'codex',
        (synthetic_adapter, lambda *a, **kw: [{'selector_id': 'fresh', 'available': True}], 'synthetic'))
    result = await harness_models.refresh_provider('codex', reason='startup', account_id=custom['id'],
        expected_receipt=context.receipt, binary='/synthetic/codex', harness_version='test', db=db)
    assert result.status == 'ready'
    assert _governed(context, tmp_path, target, _cache_receipt(db, custom['id'])) == 'Fresh'


def test_stale_lifecycle_bundle_never_publishes_or_wakes(accounts, tmp_path, monkeypatch):
    from connections.vendor_accounts import set_account_state, get_account

    db, _, custom, _ = accounts
    context, stage, root, target = _bundle(db, custom, tmp_path)
    set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    before = get_account(custom['id'], db=db)
    emitted = []
    monkeypatch.setattr('infra.state.ws.schedule_models_changed', lambda: emitted.append('models'))
    assert _commit(db, context, stage).cause == 'account_refresh_superseded'
    assert _slug(target) == 'Old'
    assert get_account(custom['id'], db=db) == before
    assert emitted == []
    stage.close()
    assert not root.exists()


@pytest.mark.asyncio
async def test_custom_single_flight_shields_waiters_and_keeps_default_provider_key(accounts, monkeypatch):
    from connections import harness_models
    from connections.account_discovery_types import StagedDiscovery
    from connections.vendor_accounts import get_account

    db, default, custom, other = accounts
    for account in (custom, other):
        _ready(db, account)
    other_before = get_account(other['id'], db=db)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def adapter(binary, *, context):
        calls.append(context)
        entered.set()
        await release.wait()
        return StagedDiscovery({'models': [context.account_id]}, usage_report={'ordinaryUsageAllowed': True})
    def normalize(payload, **kwargs):
        return [{'selector_id': 'own-' + payload['models'][0], 'available': True}]
    monkeypatch.setitem(harness_models._ADAPTERS, 'codex', (adapter, normalize, 'synthetic'))
    first = asyncio.create_task(harness_models.refresh_provider('codex', reason='manual',
        account_id=custom['id'], binary='/synthetic/codex', harness_version='test', db=db))
    await entered.wait()
    second = asyncio.create_task(harness_models.refresh_provider('codex', reason='manual',
        account_id=custom['id'], binary='/synthetic/codex', harness_version='test', db=db))
    await asyncio.sleep(0)
    assert len(calls) == 1
    key = calls[0].key
    assert harness_models._REFRESH_TASKS[key].done() is False
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not harness_models._REFRESH_TASKS[key].cancelled()
    release.set()
    result = await second
    assert result.status == 'ready' and result.account_id == custom['id']
    await asyncio.sleep(0)
    assert key not in harness_models._REFRESH_TASKS
    assert get_account(other['id'], db=db) == other_before
    assert 'codex' not in harness_models._REFRESH_TASKS


@pytest.mark.asyncio
async def test_old_generation_task_completion_cannot_clear_new_generation_task(accounts, monkeypatch):
    from connections import harness_models
    from connections.account_discovery_types import StagedDiscovery
    from connections.vendor_accounts import set_account_state, get_account
    from connections.account_receipts import capture_receipt

    db, _, custom, _ = accounts
    _ready(db, custom)
    gates = [asyncio.Event(), asyncio.Event()]
    entered = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def adapter(binary, *, context):
        index = len(calls)
        calls.append(context)
        entered[index].set()
        await gates[index].wait()
        return StagedDiscovery({'models': []})
    monkeypatch.setitem(harness_models._ADAPTERS, 'codex', (adapter, lambda *a, **kw: [], 'synthetic'))
    old = asyncio.create_task(harness_models.refresh_provider('codex', reason='manual',
        account_id=custom['id'], binary='/synthetic', harness_version='test', db=db))
    await entered[0].wait()
    latest = set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    new_receipt = capture_receipt('codex', account_id=latest['id'], db=db)
    new = asyncio.create_task(harness_models.refresh_provider('codex', reason='manual',
        account_id=custom['id'], expected_receipt=new_receipt, binary='/synthetic', harness_version='test', db=db))
    await entered[1].wait()
    assert calls[0].key != calls[1].key
    gates[0].set()
    assert (await old).cause == 'account_refresh_superseded'
    await asyncio.sleep(0)
    assert calls[1].key in harness_models._REFRESH_TASKS
    assert not harness_models._REFRESH_TASKS[calls[1].key].done()
    gates[1].set()
    assert (await new).status == 'ready'
    await asyncio.sleep(0)
    assert calls[1].key not in harness_models._REFRESH_TASKS
    assert get_account(custom['id'], db=db)['discovery']['discovery']['receipt']['login_seq'] == new_receipt.login_seq


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
async def test_actual_custom_refresh_version_uses_clean_own_verbatim_source_home(monkeypatch, provider):
    result, calls, writes = await _refresh(monkeypatch, provider)
    assert result == 'committed' and len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ('/synthetic/vendor', '--version')
    assert kwargs == {'stdout': asyncio.subprocess.PIPE, 'stderr': asyncio.subprocess.PIPE,
                      'stdin': asyncio.subprocess.DEVNULL,
                      'env': {'CLEAN': provider,
                              'CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR': '/synthetic/./source'}}
    assert writes[0][0] == 'commit_discovery_bundle'


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
async def test_actual_default_refresh_preserves_original_version_command_without_env_or_stdin(monkeypatch, provider):
    result, calls, writes = await _refresh(monkeypatch, provider, is_default=True, source_home=None)
    assert result == 'committed'
    assert calls == [(('/synthetic/vendor', '--version'),
                      {'stdout': asyncio.subprocess.PIPE, 'stderr': asyncio.subprocess.PIPE})]
    assert writes[0][0] == 'commit_discovery_bundle'


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
async def test_missing_custom_source_fails_before_any_version_process(monkeypatch, provider):
    result, calls, writes = await _refresh(monkeypatch, provider, source_home=None)
    assert result == 'account_discovery_failed' and calls == []
    assert writes[0][0] == 'record_discovery_failure'
    assert writes[0][1]['cause'] == 'account_discovery_failed'


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
async def test_custom_version_timeout_kills_and_reaps_original_process(monkeypatch, provider):
    from connections import harness_models
    from connections.account_discovery_refresh import _version_environment

    events = []
    class Process:
        returncode = None
        async def communicate(self):
            events.append('communicate')
            return b'', b''
        def kill(self):
            events.append('kill')
            self.returncode = -9
    process = Process()
    async def spawn(*argv, **kwargs):
        assert argv == ('/synthetic/vendor', '--version')
        assert kwargs['env']['CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'] == '/synthetic/./source'
        assert kwargs['stdin'] == asyncio.subprocess.DEVNULL
        return process
    async def time_out(coroutine, *, timeout):
        assert timeout == 10
        await coroutine
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(asyncio, 'wait_for', time_out)
    env = _version_environment(SimpleNamespace(provider=provider, source_home='/synthetic/./source'))
    with pytest.raises(RuntimeError, match='--version timed out'):
        await harness_models.binary_version('/synthetic/vendor', env=env)
    assert events == ['communicate', 'kill', 'communicate']
    assert process.returncode == -9


def test_metadata_controls_are_safe_and_revision_checked(client_refuses_unsigned_api, accounts):
    db, legacy, second, third = accounts
    rows = client_refuses_unsigned_api.get("/api/vendor-accounts?provider=codex").json()["accounts"]
    assert [row["id"] for row in rows] == [legacy["id"], second["id"], third["id"]]
    assert [row["label"]["text"] for row in rows] == ["ChatGPT", "Second", "Third"]
    assert all("source_home" not in row and "runtime_home" not in row and "discovery" not in row for row in rows)
    added = client_refuses_unsigned_api.post("/api/vendor-accounts", json={"provider": "codex", "name": "New"})
    assert added.status_code == 200
    assert added.json()["state"] == "signed_out"
    assert added.json()["label"]["text"] == "New"
    identity = added.json()["id"]
    renamed = client_refuses_unsigned_api.patch("/api/vendor-accounts/" + identity, json={"name": "Renamed", "expected_revision": 0})
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["revision"] == 1
    assert client_refuses_unsigned_api.post("/api/vendor-accounts/" + identity + "/signout", json={"expected_revision": 0}).status_code == 409
    deleted = client_refuses_unsigned_api.request("DELETE", "/api/vendor-accounts/" + identity, json={"expected_revision": 1})
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "removed"
    assert deleted.json()["auth_cleanup_status"] == "failed"
    assert identity not in [row["id"] for row in client_refuses_unsigned_api.get("/api/vendor-accounts").json()["accounts"]]
    assert identity in [row["id"] for row in client_refuses_unsigned_api.get("/api/vendor-accounts?include_removed=true").json()["accounts"]]


def test_api_keeps_identifier_fallback_after_an_account_is_removed(client_refuses_unsigned_api, accounts):
    db, legacy, second, third = accounts
    db.execute("UPDATE vendor_accounts SET state='removed' WHERE id=?", (second["id"],))
    db.commit()

    rows = client_refuses_unsigned_api.get("/api/vendor-accounts?provider=codex").json()["accounts"]

    assert [row["id"] for row in rows] == [legacy["id"], third["id"]]
    assert [row["label"]["text"] for row in rows] == ["ChatGPT", "Third"]


def test_api_and_delegation_share_the_s8_label_after_an_earlier_removal(client_refuses_unsigned_api, accounts):
    from llm.delegation_model import _account_label

    db, legacy, _second, third = accounts
    db.execute("UPDATE vendor_accounts SET state='removed' WHERE id=?", (legacy["id"],))
    db.commit()

    rows = client_refuses_unsigned_api.get("/api/vendor-accounts").json()["accounts"]
    api_label = next(row["label"]["text"] for row in rows if row["id"] == third["id"])

    assert api_label == _account_label(third["id"]) == "Third"


def test_ordered_policy_api_and_thread_ownership(client_refuses_unsigned_api, accounts):
    db, legacy, second, third = accounts
    legacy_route = "/api/projects/destination-project/vendor-accounts/codex"
    route = "/api/provider-policy/codex?project_id=destination-project"
    ordered = [third["id"], second["id"]]
    body = {"rollover": False, "order": ordered, "disabled": [legacy["id"]], "expected_revision": 0}
    assert client_refuses_unsigned_api.put(legacy_route, json=body).status_code == 404
    response = client_refuses_unsigned_api.put(route, json=body)
    assert response.status_code == 200
    assert response.json()["order"] == ordered
    assert response.json()["disabled"] == [legacy["id"]]
    payload = client_refuses_unsigned_api.get("/api/provider-policy?project_id=destination-project").json()
    saved = next(item for item in payload["providers"] if item["provider_key"] == "codex")
    assert saved == response.json()
    assert client_refuses_unsigned_api.put(route, json=body).status_code == 409
    assert client_refuses_unsigned_api.get("/api/projects/missing/vendor-accounts/codex").status_code == 404
    assert client_refuses_unsigned_api.put("/api/provider-policy/codex?project_id=missing", json=body).status_code == 409
    assert client_refuses_unsigned_api.get("/api/threads/destination/vendor-account").status_code == 200
    db.execute("UPDATE threads SET user_id='someone-else' WHERE id='destination'")
    db.commit()
    assert client_refuses_unsigned_api.get("/api/threads/destination/vendor-account").status_code == 404


def test_policy_put_returns_the_matching_get_provider_entry_in_workspace_and_project_scopes(
        client, accounts):
    _db, legacy, second, third = accounts
    workspace_body = {
        "rollover": True, "order": [third["id"], second["id"]], "disabled": [legacy["id"]],
        "expected_revision": 0,
    }
    workspace_response = client.put("/api/provider-policy/codex", json=workspace_body)
    assert workspace_response.status_code == 200
    workspace_entry = next(provider for provider in client.get("/api/provider-policy").json()["providers"]
                           if provider["provider_key"] == "codex")
    assert workspace_response.json()["units"] == workspace_entry["units"]
    assert workspace_response.json() == workspace_entry

    project_body = {
        "rollover": False, "order": [second["id"], third["id"]], "disabled": [legacy["id"]],
        "expected_revision": workspace_response.json()["revision"],
    }
    project_response = client.put("/api/provider-policy/codex?project_id=destination-project", json=project_body)
    assert project_response.status_code == 200
    project_payload = client.get("/api/provider-policy?project_id=destination-project").json()
    assert project_payload["use_workspace"] is False
    project_entry = next(provider for provider in project_payload["providers"] if provider["provider_key"] == "codex")
    assert project_response.json()["units"] == project_entry["units"]
    assert project_response.json() == project_entry


def test_person_account_pin_ignores_the_runtime_epoch_holds_a_busy_choice_and_keeps_vendor_refusals(
        client_refuses_unsigned_api, accounts):
    from connections.account_policy import select_account
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state

    db, legacy, second, third = accounts
    route = "/api/threads/destination/vendor-account"
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    db.execute("UPDATE threads SET runtime_context_epoch=runtime_context_epoch+5 WHERE id='destination'")
    db.commit()
    pinned = client_refuses_unsigned_api.put(route, json={"account_id": second["id"]})
    assert pinned.status_code == 200, pinned.json()
    assert pinned.json()["account_id"] == second["id"] and pinned.json()["changed"] is True
    bound = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='destination'").fetchone()
    assert bound["account_id"] == second["id"]

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/synthetic"})
    foreign = set_account_state(add_account("claude-code", "Claude Second")["id"], "signed_in", expected_revision=0)
    # Another vendor's account binds that vendor for the thread's next turn on it; the running vendor's binding stays.
    other_vendor = client_refuses_unsigned_api.put(route, json={"account_id": foreign["id"]})
    assert other_vendor.status_code == 200 and other_vendor.json()["pending"] is True
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='destination' "
                      "AND provider='codex'").fetchone()[0] == second["id"]
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='destination' "
                      "AND provider='claude-code'").fetchone()[0] == foreign["id"]

    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES ('turn','destination',0,'codex/test','running','t','boot')")
    db.commit()
    busy = client_refuses_unsigned_api.put(route, json={"account_id": third["id"]})
    # A choice made while a turn runs is held for the next claim, as a model choice is.
    assert busy.status_code == 200 and busy.json()["pending"] is True and busy.json()["changed"] is False
    assert tuple(db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                            "WHERE thread_id='destination' AND provider='codex'").fetchone()) == (second["id"], third["id"])


def test_catalog_order_and_custom_unknown_evidence_keeps_display_capabilities(accounts):
    from connections.account_policy import select_account
    from connections.provider_policy import ordered_accounts, set_policy
    from llm.account_catalog import project_accounts

    db, legacy, second, third = accounts
    ordered = [third["id"], second["id"]]
    policy = set_policy("destination-project", "codex", rollover=False, order=ordered,
                        disabled=[legacy["id"]], expected_revision=0, db=db)
    assert [account["id"] for account in ordered_accounts("codex", policy, db=db)] == ordered
    select_account("destination", "codex", db=db)
    row = {"connection": "codex", "litellm_model": "codex/test", "status": "ready", "context_window": 123456,
           "capabilities": {"reasoning_config": {"default": "high"}}, "resolved_identity": "default-only",
           "cache_marker": "Cached"}
    raw = project_accounts([row], thread_id="destination", db=db)[0]
    assert [item["account_id"] for item in raw["runs_on"]] == ordered
    assert [item["account_id"] for item in raw["runs_on"] if item["selected"]] == [third["id"]]
    assert raw["status"] == "ready"
    selected = next(item for item in raw["runs_on"] if item["selected"])
    assert selected["account_id"] == third["id"]
    assert selected["status"] == "unavailable"
    assert selected["status_reason"] == "account_model_not_discovered"
    assert raw["resolved_identity"] is None
    assert raw["context_window"] is None
    assert raw["capabilities"] == {"reasoning_config": {"default": "high"}}
    assert "usage" not in raw and "cache_marker" not in raw
    assert legacy["id"] not in str(raw)
    assert legacy["name"] not in str(raw)
    assert "default-only" not in str(raw)


def test_receipt_known_before_publication_validates_the_published_file(tmp_path):
    fresh, target = tmp_path / 'fresh', tmp_path / 'target'
    target.write_bytes(b'old model metadata')
    fresh.write_text(json.dumps({'models': ['Own']}))
    stage = ModelCacheStage.prepare(fresh, target)
    planned = stage.receipt()
    assert not validate_cache(planned, target)
    assert stage.promote() == planned
    assert validate_cache(planned, target)


def test_receipt_detects_external_replacement_even_with_equal_bytes(tmp_path):
    fresh, target = tmp_path / 'fresh', tmp_path / 'target'
    fresh.write_text(json.dumps({'models': ['Own']}))
    receipt = ModelCacheStage.prepare(fresh, target).promote()
    other = tmp_path / 'other'
    other.write_bytes(target.read_bytes())
    other.replace(target)
    assert not validate_cache(receipt, target)


def test_competing_cache_writer_invalidates_the_staged_file(tmp_path):
    fresh, target = tmp_path / 'fresh', tmp_path / 'target'
    fresh.write_bytes(b'own')
    target.write_bytes(b'old')
    stage = ModelCacheStage.prepare(fresh, target)
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'newer')
    replacement.replace(target)
    with pytest.raises(RuntimeError, match='model_cache_path_changed'):
        stage.promote()
    assert target.read_bytes() == b'newer'


def test_own_ready_evidence_replaces_foreign_model_and_usage_status(accounts):
    from connections.account_receipts import capture_receipt
    from connections.account_evidence import mark_discovery

    db, _, custom, other = accounts
    _ready(db, other)
    row = _catalog(db, custom)
    selected = next(item for item in row['runs_on'] if item['selected'])
    assert selected['status_reason'] == 'account_model_not_discovered'
    assert row['context_window'] is None
    assert row['capabilities'] == {'foreign': True}
    receipt = capture_receipt('codex', account_id=custom['id'], db=db)
    mark_discovery(custom['id'], receipt=receipt, status='ready', models={'codex/test': {
        'resolved_identity': 'Own', 'context_window': 123, 'capabilities': {'own': True}}}, db=db)
    db.commit()
    row = _catalog(db, custom)
    selected = next(item for item in row['runs_on'] if item['selected'])
    assert selected['status'] == 'ready'
    assert selected['status_reason'] is None
    assert row['resolved_identity'] == 'Own'
    assert row['context_window'] == 123
    # The row carries the resolved capability block, never the raw discovery payload.
    assert 'own' not in row['capabilities']
    assert row['capabilities']['reasoning_config']['type'] == 'effort_levels'
    assert 'usage' not in row
    own = next(item for item in row['runs_on'] if item['account_id'] == custom['id'])
    assert own['status'] == 'ready'


def test_display_capabilities_do_not_change_account_readiness_or_admission(accounts):
    from connections.account_evidence import readiness_reason
    from connections.account_policy import AccountAdmissionError, require_claim, select_account
    from connections.vendor_accounts import get_account
    from llm.account_catalog import account_model_status, effective_status

    db, _, custom, other = accounts
    _ready(db, other)
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    row = _catalog(db, custom)
    account = get_account(custom['id'], db=db)

    assert row['capabilities'] == {'foreign': True}

    def observed(candidate):
        decision = account_model_status(account, candidate, project_ids=['destination-project'], db=db)
        with pytest.raises(AccountAdmissionError) as failure:
            require_claim('codex', thread_id='destination', model='codex/test', db=db)
        return (readiness_reason(account, model='codex/test'),
                (decision['status'], decision['status_reason']),
                effective_status(candidate), candidate['runnable'], failure.value.reason)

    assert observed(row) == observed({**row, 'capabilities': {}})


def test_added_account_row_carries_the_resolved_reasoning_config(accounts):
    """An added account's row resolves its reasoning shape like every other row.

    The row used to carry the account's raw discovery record verbatim, so the
    switcher lost the resolved capability block — ``reasoning_config``
    especially — and the reasoning control disappeared on every added account.
    """
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    db, _, custom, other = accounts
    for account, caps in (
        (custom, {'reasoning_efforts': ['low', 'high'], 'default_reasoning_effort': 'high'}),
        (other, {}),
    ):
        receipt = capture_receipt('codex', account_id=account['id'], db=db)
        mark_discovery(account['id'], receipt=receipt, status='ready', models={'codex/test': {
            'resolved_identity': 'Own', 'context_window': 321, 'capabilities': caps}}, db=db)
    db.commit()

    discovered = _catalog(db, custom)
    assert next(item for item in discovered['runs_on'] if item['selected'])['status'] == 'ready'
    assert discovered['capabilities']['reasoning_config'] == {
        'type': 'effort_levels', 'values': ['low', 'high'], 'default': 'high', 'supports_auto': True,
    }
    assert discovered['capabilities']['context_window_max'] == 321

    # An added account that reported no levels keeps the connector's own shape.
    silent = _catalog(db, other)
    assert silent['capabilities']['reasoning_config'] == {
        'type': 'effort_levels',
        'values': ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
        'default': 'high',
    }


def test_ready_different_selector_and_stale_login_cannot_publish_catalog(accounts):
    from connections.vendor_accounts import set_account_state

    db, _, custom, _ = accounts
    _ready(db, custom, model='codex/other')
    row = _catalog(db, custom)
    assert next(item for item in row['runs_on'] if item['selected'])['status_reason'] == 'account_model_not_discovered'
    assert row['resolved_identity'] is None
    _ready(db, custom)
    set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    row = _catalog(db, custom)
    assert next(item for item in row['runs_on'] if item['selected'])['status_reason'] == 'account_model_not_discovered'
    assert row['resolved_identity'] is None


def test_rename_api_preserves_label_text_when_submitting_the_ui_custom_prefill(client_refuses_unsigned_api, accounts):
    """The rename field may submit only `label.custom`, never the person-facing label text."""
    from connections.models import upsert_connection
    from connections.vendor_accounts import DEFAULT_COMPARISON, default_account

    db, _, named, _ = accounts
    db.execute("UPDATE vendor_accounts SET identity_json=? WHERE id=?", (json.dumps({"email": "named@example.test"}), named["id"]))
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"}, db=db)
    unnamed = default_account("claude-code", db=db)
    db.execute("UPDATE vendor_accounts SET identity_json=? WHERE id=?", (
        json.dumps({DEFAULT_COMPARISON: {"facts": {"email": "org@example.test", "org_name": "Acme"}}}), unnamed["id"],
    ))
    db.commit()

    named_before = next(row for row in client_refuses_unsigned_api.get("/api/vendor-accounts?provider=codex").json()["accounts"] if row["id"] == named["id"])
    assert named_before["label"] == {"name": "Second", "email": "named@example.test", "text": "Second · named@example.test", "custom": "Second", "identified": True}
    renamed = client_refuses_unsigned_api.patch("/api/vendor-accounts/" + named["id"], json={"name": named_before["label"]["custom"], "expected_revision": named_before["revision"]})
    assert renamed.status_code == 200
    named_after = next(row for row in client_refuses_unsigned_api.get("/api/vendor-accounts?provider=codex").json()["accounts"] if row["id"] == named["id"])
    assert named_after["label"]["text"] == named_before["label"]["text"]

    unnamed_before = next(row for row in client_refuses_unsigned_api.get("/api/vendor-accounts?provider=claude-code").json()["accounts"] if row["id"] == unnamed["id"])
    assert unnamed_before["label"] == {"name": "Acme", "email": "org@example.test", "text": "Acme · org@example.test", "custom": None, "identified": True}
    refused = client_refuses_unsigned_api.patch("/api/vendor-accounts/" + unnamed["id"], json={"name": unnamed_before["label"]["custom"] or "", "expected_revision": unnamed_before["revision"]})
    assert refused.status_code == 409
    unnamed_after = next(row for row in client_refuses_unsigned_api.get("/api/vendor-accounts?provider=claude-code").json()["accounts"] if row["id"] == unnamed["id"])
    assert unnamed_after["label"]["text"] == unnamed_before["label"]["text"]


def test_account_labels_use_login_facts_and_the_s8_fallback():
    from connections.account_profiles import account_label
    from connections.vendor_accounts import DEFAULT_COMPARISON

    default = {
        "id": "default", "provider": "claude-code", "name": "Default", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": {"email": "default@example.test", "org_name": "Acme"}}},
        "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(default, rows=[default]) == {
        "name": "Acme", "email": "default@example.test", "text": "Acme · default@example.test", "custom": None, "identified": True,
    }

    personal_org = {
        "id": "personal-org", "provider": "claude-code", "name": "Default", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": {
            "email": "person@example.test", "org_name": "person@example.test's Organization",
        }}},
        "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(personal_org, rows=[personal_org]) == {
        "name": "person@example.test", "email": "person@example.test",
        "text": "person@example.test", "custom": None, "identified": True,
    }

    renamed = {
        "id": "work", "provider": "claude-code", "name": "Work", "source_home": "/private/work",
        "identity": {"email": "work@example.test", "org_name": "Acme"}, "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(renamed, rows=[renamed]) == {
        "name": "Work · Acme", "email": "work@example.test", "text": "Work · Acme · work@example.test", "custom": "Work", "identified": True,
    }

    known_email_without_detail = {
        "id": "plain", "provider": "codex", "name": "Default", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": {"email": "plain@example.test"}}}, "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(known_email_without_detail, rows=[known_email_without_detail]) == {
        "name": "plain@example.test", "email": "plain@example.test", "text": "plain@example.test", "custom": None, "identified": True,
    }

    codex_without_email = {
        "id": "codex-default", "provider": "codex", "name": "Default", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": {"account_fp": "fingerprint", "plan": "Plus"}}},
        "created_at": "2026-10-02T00:00:00Z",
    }
    assert account_label(codex_without_email, rows=[
        {"id": "other", "provider": "codex", "created_at": "2026-10-01T00:00:00Z"}, codex_without_email,
    ]) == {"name": "ChatGPT Plus", "email": None, "text": "ChatGPT Plus", "custom": None, "identified": True}

    codex_without_email_or_plan = {
        "id": "codex-unobserved", "provider": "codex", "name": "Default", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": {"account_fp": "fingerprint"}}},
        "created_at": "2026-10-03T00:00:00Z",
    }
    assert account_label(codex_without_email_or_plan, rows=[codex_without_email_or_plan]) == {
        "name": "ChatGPT", "email": None, "text": "ChatGPT", "custom": None, "identified": False,
    }

    custom_without_email = {
        "id": "codex-custom", "provider": "codex", "name": "Personal", "source_home": "/private/personal",
        "identity": {"plan": "Plus"}, "created_at": "2026-10-02T00:00:00Z",
    }
    assert account_label(custom_without_email, rows=[
        {"id": "other", "provider": "codex", "created_at": "2026-10-01T00:00:00Z"}, custom_without_email,
    ]) == {"name": "Personal", "email": None, "text": "Personal", "custom": "Personal", "identified": True}


@pytest.mark.parametrize(('provider', 'name', 'source_home', 'identity', 'identified'), [
    ("claude-code", "Default", None, {"email": "me@example.test"}, True),
    ("claude-code", "Work", "/private/work", {}, True),
    ("codex", "Default", None, {"plan": "Plus"}, True),
    ("codex", "Default", None, {}, False),
    ("claude-code", "Default", None, {}, False),
])
def test_account_label_identified_is_false_only_for_the_bare_vendor(provider, name, source_home, identity, identified):
    from connections.account_profiles import account_label
    from connections.vendor_accounts import DEFAULT_COMPARISON

    account = {
        "id": "a", "provider": provider, "name": name, "source_home": source_home,
        "identity": identity if source_home else {DEFAULT_COMPARISON: {"facts": identity}},
        "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(account, rows=[account])["identified"] is identified


@pytest.mark.parametrize(('identity', 'expected'), [
    ({"email": "work@example.test", "org_name": "Acme"},
     {"name": "Work · Acme", "email": "work@example.test", "text": "Work · Acme · work@example.test", "custom": "Work", "identified": True}),
    ({"plan": "Plus"}, {"name": "Work", "email": None, "text": "Work", "custom": "Work", "identified": True}),
])
def test_renamed_builtin_account_uses_its_user_name(identity, expected):
    from connections.account_profiles import account_label
    from connections.vendor_accounts import DEFAULT_COMPARISON

    builtin = {
        "id": "builtin", "provider": "claude-code", "name": "Work", "source_home": None,
        "identity": {DEFAULT_COMPARISON: {"facts": identity}}, "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(builtin, rows=[builtin]) == expected


def test_email_less_default_labels_use_vendor_and_plan_not_creation_order():
    from connections.account_profiles import account_label
    from connections.vendor_accounts import AccountError

    first = {
        "id": "first", "provider": "codex", "name": "First", "source_home": "/private/first",
        "identity": {"plan": "Plus"}, "created_at": "2026-10-01T00:00:00Z", "is_default": True,
    }
    builtin = {
        "id": "builtin", "provider": "codex", "name": "Default", "source_home": None,
        "identity": {"default_comparison": {"facts": {"plan": "Plus"}}},
        "created_at": "2026-10-02T00:00:00Z", "is_default": False,
    }
    removed = {
        "id": "removed", "provider": "codex", "name": "Removed", "source_home": "/private/removed",
        "identity": {"plan": "Plus"}, "created_at": "2026-09-30T00:00:00Z", "state": "removed",
    }
    # Display/default order cannot change an identifier fallback.
    rows = [builtin, first, removed]
    assert account_label(builtin, rows=rows)["text"] == "ChatGPT Plus"
    first["is_default"], builtin["is_default"] = False, True
    assert account_label(builtin, rows=rows)["text"] == "ChatGPT Plus"

    with pytest.raises(AccountError, match="not present"):
        account_label(builtin, rows=[first, removed])


def test_account_label_does_not_depend_on_creation_timestamp_format():
    from connections.account_profiles import account_label

    builtin = {
        "id": "builtin", "provider": "codex", "name": "Default", "source_home": None,
        "identity": {"default_comparison": {"facts": {"plan": "Plus"}}},
        "created_at": "2026-10-02T00:00:00+00:00",
    }
    added = {
        "id": "added", "provider": "codex", "name": "Added", "source_home": "/private/added",
        "identity": {"plan": "Plus"}, "created_at": "2026-10-01T00:00:00",
    }

    assert account_label(builtin, rows=[builtin, added]) == {
        "name": "ChatGPT Plus", "email": None, "text": "ChatGPT Plus", "custom": None, "identified": True,
    }


def test_user_named_default_is_not_treated_as_the_builtin_default_account():
    from connections.account_profiles import account_label

    named_default = {
        "id": "named-default", "provider": "codex", "name": "Default", "source_home": "/private/named-default",
        "identity": {"plan": "Plus"}, "created_at": "2026-10-01T00:00:00Z",
    }
    assert account_label(named_default, rows=[named_default]) == {
        "name": "Default", "email": None, "text": "Default", "custom": "Default", "identified": True,
    }


def test_the_vendors_are_exactly_the_profiles():
    from connections.account_profiles import _PROFILES, profile_for
    from connections.vendor_accounts import VENDORS

    # The agents' GitHub account is built in after the vendors and never launches inference.
    assert VENDORS == ("claude-code", "codex") and tuple(_PROFILES) == (*VENDORS, "github")
    claude, codex = profile_for("claude-code"), profile_for("codex")
    assert (claude.label, codex.label) == ("Claude Code", "Codex")
    assert (claude.home_variable, codex.home_variable) == ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
    assert claude.identity_facts == codex.identity_facts == ("email", "org_name", "org_id", "plan")
    assert (claude.comparison_keys, codex.comparison_keys) == (("email", "org_id"), ("account_fp",))
    assert codex.stored_facts == ("email", "org_name", "org_id", "plan", "account_fp")
    assert claude.stored_facts == claude.identity_facts


def test_row_operations_accept_a_vendor_only(accounts):
    from connections.account_profiles import profile_for
    from connections.vendor_accounts import AccountError, add_account, require_account_provider, require_vendor

    require_vendor("codex")
    require_account_provider("codex")
    with pytest.raises(AccountError, match="provider must be"):
        require_vendor("unregistered")
    for refused in (lambda: require_account_provider("unregistered"), lambda: add_account("unregistered", "Nope"),
                    lambda: profile_for("unregistered")):
        with pytest.raises(AccountError, match="not registered"):
            refused()


def test_add_creates_private_homes_and_no_identity(accounts, monkeypatch, tmp_path):
    from connections import account_profiles
    from connections.vendor_accounts import add_account, list_accounts

    monkeypatch.setitem(account_profiles._PROFILES, "codex",
                        replace(account_profiles.profile_for("codex"), home_root=lambda: tmp_path / "homes"))
    before = [row["id"] for row in list_accounts(provider="codex")]
    account = add_account("codex", "Work")
    root = tmp_path / "homes" / account["id"]
    assert (Path(account["source_home"]), Path(account["runtime_home"])) == (root / "source", root / "runtime")
    for directory in (root.parent, root, root / "source", root / "runtime"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert account["identity"] == {}
    assert (account["state"], account["is_default"], account["provider"]) == ("signed_out", 0, "codex")
    assert [row["id"] for row in list_accounts(provider="codex")] == [*before, account["id"]]


def test_evidence_keeps_only_the_profile_facts(accounts):
    from connections.vendor_accounts import get_account, record_evidence

    db, _, second, _ = accounts
    record_evidence(second["id"], identity={"email": "c@example.test", "login": "ignored", "plan": "pro",
                                            "account_fp": "f" * 64})
    stored = get_account(second["id"])
    assert stored["identity"] == {"email": "c@example.test", "plan": "pro", "account_fp": "f" * 64}
    assert stored["revision"] == second["revision"]


def test_evidence_with_an_expected_revision_refuses_a_changed_or_removed_account(accounts):
    from connections.vendor_accounts import (AccountError, add_account, get_account, record_evidence,
                                             rename_account, set_account_state)

    db, _, _, _ = accounts
    account = add_account("codex", "Work")
    # Omitted and matching both write, and neither bumps the revision.
    assert record_evidence(account["id"], identity={"plan": "team"}) == 0
    assert record_evidence(account["id"], identity={"email": "o@example.test"}, expected_revision=0) == 0
    assert get_account(account["id"])["identity"] == {"email": "o@example.test", "plan": "team"}
    renamed = rename_account(account["id"], "Renamed", expected_revision=0)
    before = _identity_bytes(db, account["id"])
    with pytest.raises(AccountError, match="account revision changed; refresh before acting"):
        record_evidence(account["id"], identity={"plan": "stale"}, expected_revision=0)
    assert _identity_bytes(db, account["id"]) == before
    assert record_evidence(account["id"], identity={"plan": "pro"}, expected_revision=renamed["revision"]) == 1
    removed = set_account_state(account["id"], "removed", expected_revision=1)
    before = _identity_bytes(db, account["id"])
    with pytest.raises(AccountError, match="account was removed"):
        record_evidence(account["id"], identity={"plan": "late"}, expected_revision=removed["revision"])
    assert record_evidence(account["id"], identity={"plan": "late"}) == removed["revision"]
    assert _identity_bytes(db, account["id"]) == before
    assert get_account(account["id"])["identity"] == {"email": "o@example.test", "plan": "pro"}


@pytest.mark.asyncio
async def test_built_in_sign_out_hooks_start_nothing_without_the_vendor_binary(accounts, monkeypatch):
    import asyncio
    import subprocess

    from connections.account_profiles import profile_for
    from connections.models import upsert_connection
    from connections.vendor_accounts import VENDORS, add_account

    def spawned(*args, **kwargs):
        raise AssertionError("a built-in sign-out hook must not start a process")

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawned)
    monkeypatch.setattr(subprocess, "Popen", spawned)
    monkeypatch.setattr(subprocess, "run", spawned)
    for vendor in VENDORS:
        account = add_account(vendor, "Hooked")
        cleanup = await profile_for(vendor).sign_out(
            account, Path(account["source_home"]), Path(account["runtime_home"]))
        assert cleanup == {"status": "failed", "cause": profile_for(vendor).missing_cause}


def test_scope_account_resolves_without_a_thread_or_a_write(accounts):
    from connections.account_policy import scope_account
    from connections.provider_policy import set_policy
    from connections.vendor_accounts import default_account, list_accounts, set_account_state

    db, legacy, second, third = accounts
    legacy, second = default_account("codex"), second["id"]
    assert scope_account("codex", project_id=None, db=db) == (legacy["id"], None)
    assert scope_account("codex", project_id="destination-project", db=db) == (legacy["id"], None)
    assert scope_account("codex", project_id="missing", db=db) == (None, "project_policy_missing")
    # Without a provider Default, an unordered policy selects the first enabled account.
    db.execute("UPDATE vendor_accounts SET is_default=0 WHERE connection_id IN "
               "(SELECT id FROM connections WHERE provider='claude-code')")
    db.commit()
    claude_ids = [account["id"] for account in list_accounts(provider="claude-code", db=db)]
    assert scope_account("claude-code", project_id="destination-project", db=db) == (claude_ids[0], None)
    set_policy("destination-project", "claude-code", rollover=False, order=None, disabled=claude_ids,
               expected_revision=0, db=db)
    assert scope_account("claude-code", project_id="destination-project", db=db) == (None, "account_project_denied")
    assert scope_account("claude-code", project_id=None, db=db) == (claude_ids[0], None)
    # Ordered and unordered scope policies both select their first permitted account.
    disabled = [legacy["id"], third["id"]]
    set_policy("destination-project", "codex", rollover=False, order=[second], disabled=disabled,
               expected_revision=0, db=db)
    assert scope_account("codex", project_id="destination-project", db=db) == (second, None)
    assert scope_account("codex", project_id="actor-project", db=db) == (legacy["id"], None)
    set_policy("destination-project", "codex", rollover=False, order=None, disabled=disabled,
               expected_revision=1, db=db)
    assert scope_account("codex", project_id="destination-project", db=db) == (second, None)
    set_policy("destination-project", "codex", rollover=False, order=[second], disabled=disabled,
               expected_revision=2, db=db)
    set_account_state(second, "signed_out", expected_revision=1)
    assert scope_account("codex", project_id="destination-project", db=db) == (second, "account_signed_out")
    db.commit()
    changes = db.total_changes
    scope_account("codex", project_id="destination-project", db=db)
    assert db.total_changes == changes and not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts").fetchone()[0] == 0


def test_project_policy_is_refused_for_a_provider_that_is_not_a_vendor(client, accounts):
    db, _, _, _ = accounts
    refused = client.put("/api/provider-policy/unregistered?project_id=destination-project", json={
        "rollover": False, "order": None, "disabled": [], "expected_revision": 0,
    })
    assert refused.status_code == 409
    assert refused.json()["detail"] == "provider policy is not available for this provider"
    assert db.execute("SELECT COUNT(*) FROM provider_policies").fetchone()[0] == 0


def test_a_provider_that_is_not_a_vendor_never_binds_or_launches(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch, select_account

    db, _, second, _ = accounts
    for call in (lambda: select_account("destination", "synthetic", db=db),
                 lambda: select_account("destination", "synthetic", account_id=second["id"], db=db),
                 lambda: require_launch("synthetic", thread_id="destination", db=db),
                 lambda: require_launch("synthetic", account_id=second["id"], frozen=True, db=db)):
        with pytest.raises(AccountAdmissionError) as refused:
            call()
        assert refused.value.reason == "account_provider_not_inference"
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_account_holds").fetchone()[0] == 0


def test_api_adds_a_vendor_account_without_exposing_its_homes(client, accounts):
    added = client.post("/api/vendor-accounts", json={"provider": "codex", "name": "Work"})
    assert added.status_code == 200
    body = added.json()
    assert (body["provider"], body["state"], body["is_default"], body["identity"]) == ("codex", "signed_out", 0, {})
    assert not {"source_home", "runtime_home"} & set(body)
    refused = client.post("/api/vendor-accounts", json={"provider": "unregistered", "name": "Nope"})
    assert refused.status_code == 409 and "not registered" in refused.json()["detail"]


def test_sign_out_route_calls_the_profile_hook_after_the_state_commits(client, monkeypatch):
    from connections import account_profiles
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, get_account, set_account_state

    seen = []

    async def recorder(account, source, runtime):
        seen.append((account["provider"], get_account(account["id"])["state"]))
        return {"status": "done", "cause": None}

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"})
    for provider in ("codex", "claude-code"):
        monkeypatch.setitem(account_profiles._PROFILES, provider,
                            replace(account_profiles.profile_for(provider), sign_out=recorder))
        account = add_account(provider, "Leaving")
        account = set_account_state(account["id"], "signed_in", expected_revision=0)
        left = client.post("/api/vendor-accounts/" + account["id"] + "/signout",
                           json={"expected_revision": account["revision"]})
        assert left.status_code == 200
        assert (left.json()["state"], left.json()["auth_cleanup_status"]) == ("signed_out", "done")
        assert "auth_cleanup" not in left.json()
        stored = get_account(account["id"])
        assert stored["identity"] == {}
        assert (stored["auth_cleanup"]["status"], stored["auth_cleanup"]["cause"]) == ("done", None)
    assert seen == [("codex", "signed_out"), ("claude-code", "signed_out")]


def test_a_raising_sign_out_hook_leaves_the_committed_state_and_records_a_named_failure(client, monkeypatch):
    from connections import account_profiles
    from connections.vendor_accounts import add_account, get_account, set_account_state

    async def broken(account, source, runtime):
        raise RuntimeError("vendor text that must never be stored")

    monkeypatch.setitem(account_profiles._PROFILES, "codex",
                        replace(account_profiles.profile_for("codex"), sign_out=broken))
    account = add_account("codex", "Leaving")
    account = set_account_state(account["id"], "signed_in", expected_revision=0)
    left = client.post("/api/vendor-accounts/" + account["id"] + "/signout",
                       json={"expected_revision": account["revision"]})
    assert left.status_code == 200
    body = left.json()
    assert (body["state"], body["auth_cleanup_status"], body["auth_cleanup_cause"]) == (
        "signed_out", "failed", "sign_out_hook_failed")
    assert body["identity"] == {}
    stored = get_account(account["id"])
    assert stored["state"] == "signed_out"
    assert (stored["auth_cleanup"]["status"], stored["auth_cleanup"]["cause"]) == (
        "failed", "sign_out_hook_failed")
    assert "vendor text" not in str(stored["identity"]) + str(stored["auth_cleanup"]) + left.text


def test_a_sign_out_cause_the_receipt_column_does_not_name_is_recorded_as_an_invalid_result(client, monkeypatch):
    from connections import account_profiles
    from connections.vendor_accounts import add_account, get_account, set_account_state

    async def unlisted(account, source, runtime):
        return {"status": "failed", "cause": "a_cause_no_code_names"}

    monkeypatch.setitem(account_profiles._PROFILES, "codex",
                        replace(account_profiles.profile_for("codex"), sign_out=unlisted))
    account = add_account("codex", "Leaving")
    account = set_account_state(account["id"], "signed_in", expected_revision=0)
    left = client.post("/api/vendor-accounts/" + account["id"] + "/signout",
                       json={"expected_revision": account["revision"]})
    assert left.status_code == 200
    body = left.json()
    assert (body["state"], body["auth_cleanup_status"], body["auth_cleanup_cause"]) == (
        "signed_out", "failed", "sign_out_result_invalid")
    stored = get_account(account["id"])
    assert (stored["state"], stored["identity"]) == ("signed_out", {})
    assert (stored["auth_cleanup"]["status"], stored["auth_cleanup"]["cause"]) == (
        "failed", "sign_out_result_invalid")


def test_a_non_inference_provider_registers_without_widening_vendors(synthetic):
    from connections.account_profiles import account_providers, profile_for
    from connections.vendor_accounts import VENDORS

    assert VENDORS == ("claude-code", "codex")
    assert account_providers() == ("claude-code", "codex", "github", "synthetic")
    assert profile_for("synthetic").workspace_default is True
    for vendor in VENDORS:
        assert profile_for(vendor).workspace_default is False


def test_register_refuses_duplicates_and_built_in_vendors(client, synthetic, accounts):
    from connections import account_profiles
    from connections.vendor_accounts import AccountError

    db = accounts[0]
    before = account_profiles.account_providers()
    codex = account_profiles.profile_for("codex")
    for duplicate in (account_profiles.profile_for("synthetic"),
                      replace(account_profiles.profile_for("synthetic"), provider="codex")):
        with pytest.raises(AccountError, match="already registered"):
            account_profiles.register(duplicate)
    assert account_profiles.account_providers() == before
    assert account_profiles.profile_for("codex") is codex
    # Project policy stays vendor-only: a registered provider holds rows, not a policy.
    refused = client.put("/api/provider-policy/synthetic?project_id=destination-project", json={
        "rollover": False, "order": None, "disabled": [], "expected_revision": 0,
    })
    assert refused.status_code == 409
    assert refused.json()["detail"] == "provider policy is not available for this provider"


def test_a_registered_provider_holds_rows_in_its_own_home_root(synthetic, tmp_path):
    from connections.vendor_accounts import AccountError, add_account, list_accounts, require_account_provider

    require_account_provider("synthetic")
    account = add_account("synthetic", "Bot")
    assert Path(account["source_home"]).parent.parent == tmp_path / "synthetic-homes"
    assert [row["id"] for row in list_accounts(provider="synthetic")] == [account["id"]]
    with pytest.raises(AccountError, match="not registered"):
        require_account_provider("unregistered")
    with pytest.raises(AccountError, match="not registered"):
        add_account("unregistered", "Nope")


def test_add_validates_keeps_and_never_overwrites_the_hint(synthetic):
    from connections.vendor_accounts import AccountError, add_account, get_account, record_evidence

    account = add_account("synthetic", "Bot", identity_hint={"login": "bot"})
    assert account["identity"] == {"hint": {"login": "bot"}}
    record_evidence(account["id"], identity={"login": "bot", "plan": "pro", "unknown": "ignored"})
    assert get_account(account["id"])["identity"] == {"hint": {"login": "bot"}, "login": "bot", "plan": "pro"}
    with pytest.raises(AccountError, match="takes no identity hint"):
        add_account("codex", "Work", identity_hint={"login": "x"})
    assert add_account("synthetic", "No hint")["identity"] == {}


def test_the_workspace_default_is_fenced_and_provider_gated(synthetic):
    from connections.vendor_accounts import (AccountError, add_account, clear_default_account, get_account,
                                             set_default_account)

    first = add_account("synthetic", "First")
    second = add_account("synthetic", "Second")
    vendor = add_account("codex", "Work")
    with pytest.raises(AccountError, match="cannot be changed"):
        set_default_account(vendor["id"], expected_revision=vendor["revision"])
    assert set_default_account(first["id"], expected_revision=first["revision"])["is_default"] == 1
    assert set_default_account(second["id"], expected_revision=second["revision"])["is_default"] == 1
    assert get_account(first["id"])["is_default"] == 0
    with pytest.raises(AccountError, match="revision changed"):
        set_default_account(first["id"], expected_revision=first["revision"])
    with pytest.raises(AccountError, match="not the workspace default"):
        clear_default_account(first["id"], expected_revision=get_account(first["id"])["revision"])
    assert clear_default_account(second["id"], expected_revision=get_account(second["id"])["revision"])[
        "is_default"] == 0


def test_api_takes_an_identity_hint_and_moves_the_workspace_default(client, synthetic):
    added = client.post("/api/vendor-accounts",
                        json={"provider": "synthetic", "name": "Bot", "identity_hint": {"login": "bot"}})
    assert added.status_code == 200
    account = added.json()
    assert (account["provider"], account["is_default"]) == ("synthetic", 0)
    chosen = client.put(f"/api/vendor-accounts/{account['id']}/default",
                        json={"expected_revision": account["revision"]})
    assert chosen.status_code == 200 and chosen.json()["is_default"] == 1
    stale = client.put(f"/api/vendor-accounts/{account['id']}/default",
                       json={"expected_revision": account["revision"]})
    assert stale.status_code == 409
    cleared = client.request("DELETE", f"/api/vendor-accounts/{account['id']}/default",
                             json={"expected_revision": chosen.json()["revision"]})
    assert cleared.status_code == 200 and cleared.json()["is_default"] == 0
    vendor = client.post("/api/vendor-accounts", json={"provider": "codex", "name": "Work"}).json()
    refused = client.put(f"/api/vendor-accounts/{vendor['id']}/default",
                         json={"expected_revision": vendor["revision"]})
    assert refused.status_code == 409 and "cannot be changed" in refused.json()["detail"]


def test_a_non_inference_account_never_gets_a_launch_home(synthetic):
    from connections.account_policy import AccountAdmissionError
    from connections.account_runtime import homes_for
    from connections.vendor_accounts import add_account, get_account

    account = add_account("synthetic", "Bot")
    with pytest.raises(AccountAdmissionError) as refused:
        homes_for(get_account(account["id"]))
    assert refused.value.reason == "account_provider_not_inference"


@pytest.mark.parametrize('replacement', ['regular', 'retarget', 'missing'])
def test_established_projection_change_survives_restart_and_blocks_new_home(accounts, tmp_path, replacement):
    from connections.account_policy import AccountAdmissionError
    from connections.account_projection import ensure_projection, validate_projection
    from connections.vendor_accounts import get_account

    db, custom, receipt = _setup(accounts)
    destination = _establish(db, custom, receipt)
    destination.unlink()
    if replacement == 'regular':
        destination.touch()
    elif replacement == 'retarget':
        destination.symlink_to(tmp_path / 'unowned')
    with pytest.raises(AccountAdmissionError) as refused:
        validate_projection(receipt, home=destination.parent, db=db)
    assert refused.value.reason == 'codex_auth_projection_changed'
    assert get_account(custom['id'], db=db)['discovery']['auth_projection']['failed']['cause'] == refused.value.reason
    # A new owner/home cannot bypass the account-wide durable failure, even after restart.
    fresh = tmp_path / 'entirely-new'
    with pytest.raises(AccountAdmissionError, match='codex_auth_projection_changed'):
        ensure_projection(receipt, home=fresh, kind='checkpoint', owner_turn_id='new', db=db)
    assert not fresh.exists()


def test_correct_link_with_missing_source_refuses_without_auth_read(accounts, monkeypatch):
    from connections.account_policy import AccountAdmissionError
    from connections.account_projection import validate_projection

    db, custom, receipt = _setup(accounts)
    destination = _establish(db, custom, receipt)
    (Path(custom['source_home']) / 'auth.json').unlink()
    monkeypatch.setattr(Path, 'read_text', lambda *_args, **_kwargs: pytest.fail('projection read auth bytes'))
    with pytest.raises(AccountAdmissionError) as refused:
        validate_projection(receipt, home=destination.parent, db=db)
    assert refused.value.reason == 'codex_auth_source_missing'
    assert destination.is_symlink()


def test_unknown_existing_regular_or_retargeted_path_is_never_relinked(accounts, tmp_path):
    from connections.account_policy import AccountAdmissionError

    db, custom, receipt = _setup(accounts)
    home = tmp_path / 'unknown'
    home.mkdir()
    auth = home / 'auth.json'
    auth.touch()
    before = auth.stat()
    with pytest.raises(AccountAdmissionError, match='codex_auth_projection_changed'):
        _establish(db, custom, receipt, home, 'discovery')
    assert auth.stat() == before


def _link_identity(path: Path) -> tuple:
    """The link left in place, not replaced. Reading it can move its access time, so that is left out."""
    st = path.lstat()
    return st.st_ino, st.st_dev, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, os.readlink(path)


def test_first_exact_link_can_be_adopted_after_establishment_crash(accounts, tmp_path):
    from connections.vendor_accounts import get_account

    db, custom, receipt = _setup(accounts)
    home = tmp_path / 'crash'
    home.mkdir()
    auth = home / 'auth.json'
    auth.symlink_to(Path(custom['source_home']) / 'auth.json')
    before = _link_identity(auth)
    _establish(db, custom, receipt, home, 'discovery')
    assert _link_identity(auth) == before
    assert get_account(custom['id'], db=db)['discovery']['auth_projection']['homes'][str(auth)]['phase'] == 'established'


def test_temporary_retirement_is_intentional_and_other_home_remains_established(accounts, tmp_path):
    from connections.account_projection import begin_retirement, complete_retirement, validate_projection
    from connections.vendor_accounts import get_account

    db, custom, receipt = _setup(accounts)
    live = _establish(db, custom, receipt)
    temporary = _establish(db, custom, receipt, tmp_path / 'temporary', 'checkpoint')
    assert not begin_retirement(receipt, home=temporary.parent, owner_turn_id='foreign', db=db)
    assert begin_retirement(receipt, home=temporary.parent, owner_turn_id='T', db=db)
    temporary.unlink()
    temporary.parent.rmdir()
    assert complete_retirement(receipt, home=temporary.parent, owner_turn_id='T', db=db)
    validate_projection(receipt, home=live.parent, db=db)
    record = get_account(custom['id'], db=db)['discovery']['auth_projection']
    assert str(temporary) not in record['homes']
    assert record['failed'] is None


def test_fresh_login_cleanup_is_fenced_and_never_deletes_new_projection(accounts):
    from connections.account_projection import cleanup_stale, validate_projection
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import set_account_state, get_account

    db, custom, old = _setup(accounts)
    auth = _establish(db, custom, old)
    auth.unlink()
    auth.touch()  # Actual vendor replacement; cleanup unlinks this owned path only.
    updated = set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    current = capture_receipt('codex', account_id=custom['id'], db=db)
    assert current.login_seq == old.login_seq + 1
    assert not cleanup_stale(old, quiesced=True, db=db)
    assert auth.exists()
    assert not cleanup_stale(current, quiesced=False, db=db)
    assert cleanup_stale(current, quiesced=True, db=db)
    assert not auth.exists()
    _establish(db, updated, current)
    before = _link_identity(auth)
    assert not cleanup_stale(old, quiesced=True, db=db)
    assert _link_identity(auth) == before
    validate_projection(current, home=auth.parent, db=db)
    assert get_account(custom['id'], db=db)['discovery']['auth_projection']['stale'] == []
