"""The per-account operation homes: where a login, a cache and a receipt actually live.

Pins: warm-home guarding and the warm login source, cold checkpoint receipts, Stop effects
during an account failure, live/default launch receipts, Codex meter receipts, paid-boundary
delivery settlement, factual usage receipts, observation fences, attribution and the login
provenance stamped on a Turn."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from subagents.vendor_write_admission import prepare_session_launch

from connections.account_evidence import mark_discovery
from connections.account_operation import require_same_operation_home
from connections.account_policy import AccountAdmissionError, select_account
from connections.account_receipts import capture_receipt, ReceiptFence
from connections.account_runtime import LaunchHomes, validate_launch
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, default_account, get_account, set_account_state
from core.fork_account import admit_fork, require_fork_write
from core.checkpoint_cold_fallback import invoke_with_cold_correction
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.checkpoint_types import CheckpointProviderForkRequest
from core.fork_types import (
    ProviderForkRequest,
    ProviderForkResult,
    WarmForkUnusable,
)
from infra.database import get_db
from infra.input_admission import accept_thread_input
from infra.thread_inputs import start_idle_turn
from tests.test_checkpoint_store import _request, _seed_thread
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _known_default,
    _ready,
    _start,
    accounts,
    claude,
    default_id,
    default_receipt,
    KEY,
    ready,
    receipt_of,
    seed_turn_on_default,
    signed_in_default,
    vendors,
)


MEASURED = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def _seed(provider, db):
    upsert_connection(type="cli", provider=provider, status="available", config={}, db=db)
    model = provider + "/account-test"
    db.execute("""INSERT INTO models
        (id,harness,provider,provider_model_id,litellm_model,display_name,created_at,updated_at)
        VALUES (?,?,?,?,?,?,'t0','t0')""", (model, provider, provider, "account-test", model, "Test"))
    return model, signed_in_default(provider, db=db, models=(model,))["id"]


def _metered_session(db, account):
    from connections.account_receipts import capture_receipt

    _ready(db, account)
    return SimpleNamespace(vendor_account_id=account['id'], vendor_account_bound=True,
                           vendor_account_receipt=capture_receipt('codex', account_id=account['id'], db=db))


@pytest.fixture
def db(isolated_db):
    from connections.models import upsert_connection
    from connections.vendor_accounts import default_account
    from infra.database import get_db

    upsert_connection(type='cli', provider='codex', status='available', config={'binary': '/bin/test'})
    signed_in_default('codex')
    connection = get_db()
    yield connection
    connection.close()


def _launch(db, receipt):
    from connections.account_runtime import homes_for

    account = get_account(default_id('codex', db=db), db=db)
    validate_launch(account, launch=homes_for(account), receipt=receipt, model='codex/test', db=db)


def _sign_in_again(db):
    """The account's next login: a new login sequence under the same row."""
    identity = default_id('codex', db=db)
    set_account_state(identity, 'signed_in', expected_revision=get_account(identity, db=db)['revision'], db=db)
    db.commit()


def _launched_session(vendors, provider, tmp_path):
    from connections.model_cache_stage import ModelCacheStage
    from subagents.cc_stream import CCStreamSession
    from subagents.codex_app_server import CodexAppServerSession

    db, rows = vendors
    row = rows[provider]
    model = provider + '/test'
    if provider == 'claude-code':
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
            "VALUES ('claude-code/test','Test','claude-code/test','claude-code','test','claude-code','t','t')")
    db.execute("UPDATE threads SET model=? WHERE id='destination'", (model,))
    db.commit()
    select_account('destination', provider, account_id=row['id'], db=db)
    source = Path(row['source_home'])
    source.mkdir(parents=True, exist_ok=True)
    cache = None
    if provider == 'codex':
        source.joinpath('auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'}}))
        fresh = tmp_path / 'models.json'
        fresh.write_text(json.dumps({'models': [{'slug': 'test'}]}))
        stage = ModelCacheStage.prepare(fresh, Path(row['runtime_home']) / 'models_cache.json')
        cache = stage.promote()
    receipt = capture_receipt(provider, account_id=row['id'], db=db)
    mark_discovery(row['id'], receipt=receipt, status='ready', models={model: {'available': True}}, cache_receipt=cache, db=db)
    db.commit()
    _accept(db, 'input')
    assert _start(db, 'frozen') is not None
    if provider == 'claude-code':
        session = CCStreamSession(binary='/synthetic/claude', session_id='synthetic', corsia_thread_id='destination', model='test')
    else:
        session = CodexAppServerSession(binary='/synthetic/codex', task=None, corsia_thread_id='destination', model='test', catalog_model_id=model)
    session.corsia_turn_identity = SimpleNamespace(turn_id='frozen')
    return db, row, session


def _target(provider):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(catalog_id=provider + "/test", harness=provider, provider=provider,
                           backend="cc" if provider == "claude-code" else "codex", transport_model="test",
                           expected_reported_model=None, context_window=200000, available=True,
                           availability_reason=None, display_name="Test", supports_reasoning=False)


def _frozen_batch(db):
    from infra.thread_inputs import _batch
    from infra.vendor_login import read_turn_receipt

    turn = db.execute("SELECT * FROM active_turns WHERE id='frozen'").fetchone()
    rows = db.execute("SELECT * FROM messages WHERE active_turn_id='frozen' AND input_order IS NOT NULL").fetchall()
    return _batch('destination', 'frozen', turn['turn_order'], turn['execution_epoch'],
                  turn['model'], rows, receipt=read_turn_receipt('frozen', db=db))


def test_turn_freeze_uses_the_same_catalog_id_precedence_as_dispatch(isolated_db):
    from llm.resolution import resolve_execution_target
    db = get_db()
    model, account = _seed("codex", db)
    db.execute("""INSERT INTO models
        (id,harness,provider,provider_model_id,litellm_model,display_name,created_at,updated_at)
        VALUES ('transport-shadow','native','api','shadow',?,'Shadow','t0','t0')""", (model,))
    _seed_thread(db, "worker", model=model)
    assert resolve_execution_target(model, db=db).harness == "codex"
    accept_thread_input("worker", role="user", content="go", intent="apply_now", message_id="input", db=db)
    db.commit()
    start_idle_turn("worker", turn_id="turn", boot_epoch="boot", started_at="t1", db=db)
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='turn'").fetchone()[0] == account
    db.close()


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_turn_receipt_ignores_later_binding_and_supplied_plan_account(isolated_db, provider):
    from infra.llm_usage import record_llm_usage
    from infra.turn_usage_receipts import PLAN_FIELDS
    db = get_db()
    model, first = _seed(provider, db)
    second = add_account(provider, "Work", db=db)["id"]
    _seed_thread(db, "worker", model=model)
    accept_thread_input("worker", role="user", content="go", intent="apply_now", message_id="input", db=db)
    db.commit()
    assert start_idle_turn("worker", turn_id="turn", boot_epoch="boot", started_at="t1", db=db)
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='turn'").fetchone()[0] == first
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='worker'", (second,))
    set_account_state(first, "removed", expected_revision=get_account(first, db=db)["revision"], db=db)
    db.commit()
    record_llm_usage("worker", model, MEASURED, turn_id="turn", usage_key="paid:1",
                     binding={**dict.fromkeys(PLAN_FIELDS), "vendor_account_id": second}, vendor_account_id=second)
    # Replaying paid truth does not reattribute or duplicate it.
    record_llm_usage("worker", model, MEASURED, turn_id="turn", usage_key="paid:1")
    rows = db.execute("SELECT vendor_account_id FROM llm_usage WHERE turn_id='turn'").fetchall()
    assert [row[0] for row in rows] == [first]
    assert db.execute("SELECT model FROM threads WHERE id='worker'").fetchone()[0] == model
    db.close()


def test_pending_receipt_preserves_account_across_projection_failure(isolated_db, monkeypatch):
    from infra import llm_usage
    from infra.turn_usage_receipts import reconcile_pending_turn_usage
    db = get_db()
    model, first = _seed("codex", db)
    _seed_thread(db, "worker", model=model)
    accept_thread_input("worker", role="user", content="go", intent="apply_now", message_id="input", db=db)
    db.commit()
    start_idle_turn("worker", turn_id="turn", boot_epoch="boot", started_at="t1", db=db)
    def broken(*args, **kwargs):
        raise RuntimeError("ledger temporarily locked")
    with monkeypatch.context() as patch:
        patch.setattr(llm_usage, "project_turn_usage_receipt", broken)
        llm_usage.record_llm_usage("worker", model, MEASURED, turn_id="turn", usage_key="paid:1")
    payload = json.loads(db.execute("SELECT usage_receipts_json FROM active_turns WHERE id='turn'").fetchone()[0])
    assert next(iter(payload.values()))["vendor_account_id"] == first
    second = add_account("codex", "Work", db=db)["id"]
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='worker'", (second,))
    db.commit()
    reconcile_pending_turn_usage(db=db)
    from infra.retained_usage import usage_settlement
    from infra.database import write_transaction
    with usage_settlement(None) as settlement:
        with write_transaction(None, scope="test_settle_paid_usage") as tx:
            settlement.settle(tx, "worker")
    assert db.execute("SELECT vendor_account_id FROM llm_usage").fetchone()[0] == first
    db.close()


@pytest.mark.asyncio
async def test_cross_vendor_checkpoint_freezes_actual_writer_not_source_turn(isolated_db):
    from core.checkpoint_types import CheckpointProviderForkRequest
    from core.checkpoint_usage import bind_usage
    from infra import checkpoints
    from llm.resolution import ExecutionTarget
    db = get_db()
    source_model, source = _seed("claude-code", db)
    writer_model, writer = _seed("codex", db)
    _seed_thread(db, "worker", model=source_model)
    db.execute("INSERT INTO active_turns(id,thread_id,model,execution_epoch,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('turn-1','worker',?,0,'running','t0','boot',?)", (source_model, source))
    cp = replace(_request("worker", "cp", source_harness="claude-code", source_model=source_model),
                 writer_harness="codex", writer_model=writer_model, mode="cold")
    checkpoints.create_running(cp, db=db)
    db.commit()
    target = ExecutionTarget(writer_model, "codex", "codex", "external", "test", None, None, True, None, "Test", False)
    request = CheckpointProviderForkRequest("cp", "worker", target, "summarize", None, "/tmp")
    bound = await bind_usage(request)
    second = add_account("codex", "Later", db=db)["id"]
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('worker','codex',?)", (second,))
    set_account_state(writer, "removed", expected_revision=get_account(writer, db=db)["revision"], db=db)
    db.commit()
    await bound.on_usage("paid:1", writer_model, MEASURED)
    row = db.execute("SELECT turn_id,vendor_account_id FROM llm_usage WHERE source='checkpoint'").fetchone()
    assert tuple(row) == (None, writer)
    assert writer != source
    db.close()


def test_direct_writer_account_and_unknown_native_remain_distinct(isolated_db):
    from infra.llm_usage import record_llm_usage
    db = get_db()
    model, account = _seed("codex", db)
    _seed_thread(db, "worker", model="native/unknown")
    record_llm_usage("worker", model, MEASURED, vendor_account_id=account)
    record_llm_usage("worker", "native/unknown", MEASURED)
    rows = db.execute("SELECT model,vendor_account_id,turn_id FROM llm_usage ORDER BY rowid").fetchall()
    assert [tuple(row) for row in rows] == [(model, account, None), ("native/unknown", None, None)]
    db.close()


@pytest.mark.parametrize("projected", [False, True])
def test_replay_rejects_changed_account_truth_before_or_after_projection(isolated_db, projected):
    from infra.turn_usage_receipts import (UsageReceiptConflictError, project_turn_usage_receipt,
                                           store_turn_usage_receipt)
    db = get_db()
    model, first = _seed("codex", db)
    second = add_account("codex", "Other", db=db)["id"]
    _seed_thread(db, "worker", model=model)
    accept_thread_input("worker", role="user", content="go", intent="apply_now", message_id="input", db=db)
    db.commit()
    start_idle_turn("worker", turn_id="turn", boot_epoch="boot", started_at="t1", db=db)
    kwargs = dict(thread_id="worker", turn_id="turn", usage_key="paid", model=model,
                  measured=MEASURED, ttft_ms=None, source="conversation", now_iso="t1")
    identity, _, _ = store_turn_usage_receipt(db, **kwargs)
    if projected:
        project_turn_usage_receipt(db, "turn", identity)
    # Simulate corrupt producer truth; canonical runtime never mutates this column.
    db.execute("UPDATE active_turns SET vendor_account_id=? WHERE id='turn'", (second,))
    db.commit()
    with pytest.raises(UsageReceiptConflictError):
        store_turn_usage_receipt(db, **kwargs)
    if projected:
        assert db.execute("SELECT vendor_account_id FROM llm_usage").fetchone()[0] == first
    db.close()


def test_direct_held_usage_keeps_actual_account_after_removal(isolated_db, monkeypatch):
    from infra import llm_usage
    from infra.database import write_transaction
    from infra.retained_usage import usage_settlement
    db = get_db()
    model, account = _seed("codex", db)
    _seed_thread(db, "worker", model="native/unknown")
    def broken(*args, **kwargs):
        raise RuntimeError("ledger temporarily unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(llm_usage, "_write_direct_usage", broken)
        llm_usage.record_llm_usage("worker", model, MEASURED, vendor_account_id=account)
    set_account_state(account, "removed", expected_revision=get_account(account, db=db)["revision"], db=db)
    db.commit()
    with usage_settlement(None) as settlement:
        with write_transaction(None, scope="test_settle_direct_usage") as tx:
            settlement.settle(tx, "worker")
    row = db.execute("SELECT vendor_account_id,turn_id FROM llm_usage").fetchone()
    assert tuple(row) == (account, None)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
@pytest.mark.parametrize('supersede', [False, True])
async def test_mismatch_cold_correction_preserves_original_login(vendors, tmp_path, monkeypatch, provider, supersede):
    from connections.vendor_accounts import set_account_state
    from core import checkpoint_cold_fallback

    db, row, _ = _launched_session(vendors, provider, tmp_path)
    calls, adopted, writes = [], [], []
    request = CheckpointProviderForkRequest('checkpoint', 'destination', _target(provider), 'instruction', None,
        str(tmp_path), warm_source=object(), vendor_account_id=row['id'], vendor_account_bound=True)
    async def cold_builder():
        if supersede:
            set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
        return SimpleNamespace(provider_request=replace(request, warm_source=None, cold_source=object()))
    frozen = admit_fork(replace(request, same_model_cold=cold_builder))
    async def adopt(_fn, checkpoint_id):
        adopted.append(checkpoint_id)
    monkeypatch.setattr(checkpoint_cold_fallback, 'run_in_db', adopt)
    async def invoke(candidate, _cancel):
        calls.append(candidate)
        candidate = admit_fork(candidate)
        require_same_operation_home(candidate, str(tmp_path / 'other-account'))
        assert candidate.vendor_account_receipt == frozen.vendor_account_receipt
        assert candidate.vendor_launch == frozen.vendor_launch
        if provider == 'codex':
            from connections.account_operation import prepare_operation_home

            candidate = prepare_operation_home(candidate, tmp_path / 'cold-home', kind='checkpoint')
        require_fork_write(candidate)
        writes.append(candidate.vendor_account_id)
        return ProviderForkResult('cold body', None)
    if supersede:
        with pytest.raises(AccountAdmissionError):
            await invoke_with_cold_correction(frozen, asyncio.Event(), invoke)
        assert writes == []
    else:
        result = await invoke_with_cold_correction(frozen, asyncio.Event(), invoke)
        assert result.raw_body == 'cold body' and writes == [row['id']]
    assert len(calls) == 2 and calls[1].warm_source is None
    assert calls[1].vendor_account_receipt == frozen.vendor_account_receipt
    assert adopted == ['checkpoint']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'binding', 'login'])
async def test_actual_execute_freezes_before_cold_await_and_spend_stays_on_writer(vendors, tmp_path, monkeypatch, change):
    import json
    from unittest.mock import AsyncMock
    from connections.account_operation import bind_operation_session
    from connections.vendor_accounts import set_account_state, resolve_account_id
    from core import checkpoint_runtime
    from core.checkpoint_usage import bind_usage
    from infra import checkpoints
    from subagents.cc_stream import CCStreamSession
    from tests.test_checkpoint_runtime import _checkpoint

    db, row, _ = _launched_session(vendors, 'claude-code', tmp_path)
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
        "VALUES ('boundary','destination','assistant','synthetic history','t',1)")
    db.commit()
    checkpoints.create_running(replace(_checkpoint('destination', 'checkpoint'),
        through_seq=1, through_message_id='boundary', originating_turn_id='frozen', first_included_seq=None,
        source_harness='claude-code', source_model='claude-code/test',
        writer_harness='claude-code', writer_model='claude-code/test'), db=db)
    calls, writes = [], []
    request = CheckpointProviderForkRequest('checkpoint', 'destination', _target('claude-code'),
        'instruction', None, str(tmp_path), warm_source=object())
    async def cold_builder():
        await asyncio.sleep(0)
        if change == 'login':
            from connections.account_evidence import mark_discovery
            from connections.account_receipts import capture_receipt

            set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
            current = capture_receipt('claude-code', account_id=row['id'], db=db)
            assert mark_discovery(row['id'], receipt=current, status='ready',
                models={'claude-code/test': {'available': True}}, db=db)
            db.commit()
        elif change == 'binding':
            default = signed_in_default('claude-code', db=db)['id']
            db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='destination' AND provider='claude-code'", (default,))
            db.commit()
        return SimpleNamespace(provider_request=replace(request, warm_source=None, cold_source=object()))
    request = replace(request, same_model_cold=cold_builder)
    async def invoke(candidate, _cancel):
        calls.append(candidate)
        candidate = await bind_usage(candidate)
        candidate = admit_fork(candidate)
        require_same_operation_home(candidate, str(tmp_path / 'other-account'))
        require_fork_write(candidate)
        session = CCStreamSession(binary='/synthetic/claude', session_id='synthetic',
            corsia_thread_id='destination', model='test')
        session.fork_id = 'checkpoint'
        bind_operation_session(candidate, session, kind='checkpoint')
        session._proc = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), returncode=None)
        await session.feed('cold user')
        await candidate.on_usage('cold', candidate.target.catalog_id, {
            'prompt_tokens': 3, 'completion_tokens': 2, 'cache_read_tokens': 0,
            'cache_creation_tokens': 0, 'total_tokens': 5})
        return ProviderForkResult('cold body', None)
    async def capture(_identity, result):
        assert result.raw_body == 'cold body'
        return SimpleNamespace(status='captured')
    async def notify(_record):
        pass
    monkeypatch.setattr(checkpoint_runtime, '_invoke', invoke)
    monkeypatch.setattr(checkpoint_runtime, 'CHECKPOINT_KIND', replace(CHECKPOINT_KIND, end_handler=capture))
    monkeypatch.setattr(checkpoint_runtime, '_notify_completion', notify)
    result = await checkpoint_runtime._execute('checkpoint', request, asyncio.Event())
    assert result.status == ('failed' if change == 'login' else 'captured'), getattr(result, 'error', None)
    if change == 'login':
        assert result.error is not None and 'account_login_unverified' in result.error, result.error
    assert len(calls) == 2 and calls[0].vendor_account_id == calls[1].vendor_account_id == row['id']
    assert calls[0].vendor_account_receipt == calls[1].vendor_account_receipt
    assert len(writes) == (0 if change == 'login' else 1)
    payload = json.loads(db.execute("SELECT usage_json FROM checkpoints WHERE id='checkpoint'").fetchone()[0])
    assert payload['vendor_account_id'] == row['id']
    ledger = db.execute("SELECT vendor_account_id,turn_id FROM llm_usage WHERE source='checkpoint'").fetchall()
    assert [(item['vendor_account_id'], item['turn_id']) for item in ledger] == ([] if change == 'login' else [(row['id'], None)])


@pytest.mark.asyncio
async def test_other_meter_frames_do_not_overwrite_plan_snapshot_verdict_or_reset(accounts):
    from connections import harness_usage as usage
    from subagents.codex_usage_observer import handle_rate_limits_notification, observe_turn_error

    db, _, custom, _ = accounts
    session = _metered_session(db, custom)
    usage.record_rate_limits_read({'ordinaryUsageAllowed': True, 'rateLimits': {'limitId': 'plan-meter'}},
        provider='codex', receipt=session.vendor_account_receipt, db=db)
    await handle_rate_limits_notification(session, 'account/rateLimits/updated', {'rateLimits': {
        'limitId': 'plan-meter', 'primary': {'usedPercent': 100, 'resetsAt': 2000000000},
        'rateLimitReachedType': 'plan_limit'}})
    plan = dict(session._rate_limits)
    before = usage.active_limit(provider='codex', account_id=custom['id'], db=db)
    await handle_rate_limits_notification(session, 'account/rateLimits/updated', {'rateLimits': {
        'limitId': 'other-meter', 'primary': {'usedPercent': 1, 'resetsAt': 2100000000},
        'rateLimitReachedType': None, 'planType': 'foreign'}})
    assert session._rate_limits == plan
    assert usage.active_limit(provider='codex', account_id=custom['id'], db=db) == before
    stored = usage.stored_usage(provider='codex', account_id=custom['id'], db=db)
    assert stored['facts']['limitId'] == 'plan-meter'
    assert stored['facts'].get('planType') != 'foreign'
    assert any(window.get('limit_id') == 'other-meter' for window in stored['windows'].values())
    await observe_turn_error(session, {'codexErrorInfo': 'usageLimitExceeded'})
    assert usage.active_limit(provider='codex', account_id=custom['id'], db=db)['resets_at'] == before['resets_at']


@pytest.mark.asyncio
async def test_late_old_session_callbacks_and_unbound_frames_have_no_new_login_effect(accounts):
    from connections import harness_usage as usage
    from connections.vendor_accounts import set_account_state
    from subagents.codex_usage_observer import handle_rate_limits_notification, observe_turn_success, observe_turn_error

    db, _, custom, _ = accounts
    session = _metered_session(db, custom)
    set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    before = usage.stored_usage(provider='codex', account_id=custom['id'], db=db)
    for stale in (session, SimpleNamespace()):
        await handle_rate_limits_notification(stale, 'account/rateLimits/updated', {'rateLimits': {
            'primary': {'usedPercent': 100}, 'rateLimitReachedType': 'plan_limit'}})
        await observe_turn_error(stale, {'codexErrorInfo': 'usageLimitExceeded'})
        from datetime import datetime, timezone

        await observe_turn_success(stale, datetime.now(timezone.utc))
    assert usage.stored_usage(provider='codex', account_id=custom['id'], db=db) == before


@pytest.mark.parametrize('receipt', ['missing', 'before-observation', 'other-revision'])
def test_a_default_launches_only_on_its_current_login(db, receipt):
    first_login = default_receipt('codex', db=db)
    _sign_in_again(db)
    current = default_receipt('codex', db=db)
    assert current is not None and current.login_seq == first_login.login_seq + 1
    from connections.account_projection import ensure_projection
    from connections.account_runtime import homes_for
    ensure_projection(current, home=Path(homes_for(get_account(current.account_id, db=db)).codex_runtime), kind='live', db=db)
    _launch(db, current)
    stale = {'missing': None, 'before-observation': first_login,
             'other-revision': ReceiptFence(current.account_id, current.account_revision + 1, current.login_seq)}[receipt]
    with pytest.raises(AccountAdmissionError, match='account_login_unverified'):
        _launch(db, stale)


@pytest.mark.parametrize('lost_anchor', [False, True])
def test_factual_usage_survives_new_login_and_lost_anchor_without_capacity_release(accounts, lost_anchor):
    from vendor_account_fixtures import record_default_identity
    from connections import harness_usage
    from infra.llm_usage import record_llm_usage
    from infra.vendor_login import read_turn_receipt

    db, default, other, _ = accounts
    _known_default(db, default)
    _accept(db, 'initial')
    batch = _start(db, 'T1')
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id='initial'")
    db.commit()
    record_default_identity('codex', {'account_fp': 'synthetic-new-login'}, db=db)
    if lost_anchor:
        db.execute("UPDATE messages SET active_turn_id=NULL WHERE id='initial'")
        db.commit()
        assert read_turn_receipt('T1', db=db) is None
    else:
        assert read_turn_receipt('T1', db=db) == batch.vendor_receipt
    capacity = harness_usage.stored_usage(provider='codex', account_id=default['id'], db=db)
    measured = {'prompt_tokens': 5, 'completion_tokens': 7, 'total_tokens': 12}
    for _ in range(2):
        record_llm_usage('destination', 'codex/test', measured, turn_id='T1', usage_key='factual-call',
                         vendor_account_id=other['id'], _emit_product_event=lambda *a, **kw: None)
    rows = db.execute('SELECT thread_id,turn_id,vendor_account_id,prompt_tokens,completion_tokens,total_tokens '
                      'FROM llm_usage WHERE turn_id=?', ('T1',)).fetchall()
    assert [tuple(row) for row in rows] == [('destination', 'T1', default['id'], 5, 7, 12)]
    assert harness_usage.stored_usage(provider='codex', account_id=default['id'], db=db) == capacity


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['cc-feed', 'turn/start', 'turn/steer'])
async def test_own_frozen_home_writes_once_then_same_row_new_login_writes_nothing(vendors, tmp_path, method):
    provider = 'claude-code' if method == 'cc-feed' else 'codex'
    db, row, session = _launched_session(vendors, provider, tmp_path)
    prepare_session_launch(session, provider)
    env = session._build_env() if provider == 'claude-code' else session.build_env()
    assert env['CLAUDE_CONFIG_DIR' if provider == 'claude-code' else 'CODEX_HOME'] == (
        row['source_home'] if provider == 'claude-code' else row['runtime_home'])
    writes, observed, accepted = [], [], []
    def write(data):
        writes.append(data)
        if provider == 'codex':
            identity = json.loads(data)['id']
            asyncio.get_running_loop().call_soon(session._pending[identity].set_result, {})
    session._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()), returncode=None)
    async def paid():
        if provider == 'claude-code':
            await session.feed('input', on_write_observed=lambda: observed.append(True), on_accepted=lambda: accepted.append(True))
        else:
            await session._request(method, {'input': 'input'}, on_write_observed=lambda: observed.append(True),
                                   on_drain_confirmed=lambda: accepted.append(True))
    await paid()
    assert len(writes) == len(observed) == len(accepted) == 1
    set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
    with pytest.raises(AccountAdmissionError):
        await paid()
    assert len(writes) == len(observed) == len(accepted) == 1


@pytest.mark.asyncio
async def test_codex_replacement_auth_file_is_a_link_change_and_cannot_write(vendors, tmp_path):
    db, row, session = _launched_session(vendors, 'codex', tmp_path)
    prepare_session_launch(session, 'codex')
    projected = Path(session.codex_home) / 'auth.json'
    projected.unlink()
    projected.write_text('synthetic vendor replacement')
    writes = []
    session._proc = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), returncode=None)
    with pytest.raises(AccountAdmissionError, match='codex_auth_projection_changed'):
        await session._request('turn/start', {'input': 'input'})
    assert writes == [] and session._pending == {}


def test_later_logins_never_reuse_a_prior_login_generation_and_clear_the_prior_login_usage(accounts):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt, receipt_is_current
    from connections.harness_usage import record_limited, stored_usage
    from vendor_account_fixtures import signed_in_default

    db, default, _, _ = accounts
    default = signed_in_default('codex', db=db)
    old = capture_receipt('codex', account_id=default['id'], db=db)
    record_limited(provider='codex', receipt=old, reason='plan', resets_at=None, source='synthetic', db=db)
    limited = stored_usage(provider='codex', account_id=default['id'], db=db)
    seen = [old.login_seq]
    for _ in range(2):  # A, then B, then A again: each sign-in is a new generation
        set_account_state(default['id'], 'signed_out', expected_revision=get_account(default['id'], db=db)['revision'], db=db)
        assert capture_receipt('codex', account_id=default['id'], db=db) is None
        set_account_state(default['id'], 'signed_in', expected_revision=get_account(default['id'], db=db)['revision'], db=db)
        seen.append(capture_receipt('codex', account_id=default['id'], db=db).login_seq)
    new = capture_receipt('codex', account_id=default['id'], db=db)
    assert seen == sorted(set(seen)) and new.login_seq > old.login_seq
    assert not receipt_is_current(old, db=db)
    assert not mark_discovery(default['id'], receipt=old, status='ready', models={'codex/test': {}}, db=db)
    assert limited['vendor_verdicts']
    assert not (stored_usage(provider='codex', account_id=default['id'], db=db) or {}).get('vendor_verdicts')  # a sign-in supersedes it
    assert mark_discovery(default['id'], receipt=new, status='ready', models={'codex/test': {}}, db=db)


def test_new_explicit_custom_login_clears_receipt_and_retains_projection_cleanup(accounts):
    from connections.account_receipts import account_receipt
    from connections.account_lifecycle import change_state
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    first = get_account(custom['id'], db=db)
    receipt = account_receipt(first)
    assert receipt is not None
    record = first['discovery']
    record['discovery'].update(status='ready', receipt={'account_revision': receipt.account_revision,
                                                    'login_seq': receipt.login_seq}, models={'codex/test': {}})
    record['auth_projection']['homes']['/owned/runtime/auth.json'] = {
        'source': '/owned/source/auth.json', 'destination': '/owned/runtime/auth.json',
        'home': '/owned/runtime', 'kind': 'live', 'account_revision': receipt.account_revision,
        'login_seq': receipt.login_seq, 'phase': 'established', 'owner_turn_id': None,
        'owner_boot_epoch': None, 'at': 't'}
    db.execute('UPDATE vendor_accounts SET discovery_json=? WHERE id=?', (json.dumps(record), custom['id']))
    db.commit()
    signed_out = change_state(custom['id'], 'signed_out', expected_revision=custom['revision'], db=db)
    second = change_state(custom['id'], 'signed_in', expected_revision=signed_out['revision'], db=db)
    new = account_receipt(second)
    assert new.login_seq == receipt.login_seq + 1
    assert second['discovery']['discovery']['models'] == {}
    projection = second['discovery']['auth_projection']
    assert projection['homes'] == {}
    assert projection['stale'] == [{'source': '/owned/source/auth.json', 'destination': '/owned/runtime/auth.json',
        'home': '/owned/runtime', 'kind': 'live', 'old_login_seq': receipt.login_seq,
        'cleanup_login_seq': new.login_seq, 'cleanup_revision': new.account_revision,
        'phase': 'pending', 'at': projection['stale'][0]['at']}]


def test_sign_in_rollback_changes_no_generation_or_evidence(accounts):
    from infra.database import write_transaction
    from connections.vendor_accounts import get_account
    from vendor_account_fixtures import signed_in_default

    db, default, _, _ = accounts
    signed_in_default('codex', db=db)
    before = get_account(default['id'], db=db)
    with pytest.raises(RuntimeError, match='rollback'):
        with write_transaction(db, scope='test_sign_in_rollback'):
            set_account_state(default['id'], 'signed_in', expected_revision=before['revision'], db=db)
            raise RuntimeError('rollback')
    assert get_account(default['id'], db=db) == before


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
@pytest.mark.parametrize('operation', ['checkpoint', 'detached'])
def test_admitted_operation_freezes_writer_homes_and_rejects_same_row_new_login(vendors, tmp_path, provider, operation):
    from connections.vendor_accounts import set_account_state
    from core.checkpoint_types import CheckpointProviderForkRequest
    from core.fork_account import admit_fork, require_fork_write
    from llm.isolated_structured import IsolatedStructuredRequest
    from llm.isolated_admission import admit_structured, require_structured_write

    db, row, _ = _launched_session(vendors, provider, tmp_path)
    target = _target(provider)
    if operation == 'checkpoint':
        request = CheckpointProviderForkRequest('checkpoint', 'destination', target, 'instruction', None,
            str(tmp_path), vendor_account_id=row['id'], vendor_account_bound=True)
        frozen = admit_fork(request)
        validate = lambda: require_fork_write(frozen)
    else:
        request = IsolatedStructuredRequest(system_prompt='system', user_prompt='user', output_schema={},
            max_output_tokens=32, thread_id='destination', vendor_account_id=row['id'], vendor_account_bound=True)
        frozen = admit_structured(target, request)
        validate = lambda: require_structured_write(target, frozen)
    assert frozen.vendor_account_id == row['id'] and frozen.vendor_account_receipt is not None
    assert frozen.vendor_launch.claude_dir == (row['source_home'] if provider == 'claude-code' else None)
    assert frozen.vendor_launch.codex_source == (row['source_home'] if provider == 'codex' else None)
    if provider == 'codex':
        from connections.account_operation import prepare_operation_home

        frozen = prepare_operation_home(frozen, tmp_path / operation, kind=operation, model=target.catalog_id)
    validate()
    set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
    with pytest.raises(AccountAdmissionError):
        validate()


@pytest.mark.asyncio
async def test_checkpoint_claude_spawn_uses_writer_home_and_rechecks_before_stdin(vendors, tmp_path, monkeypatch):
    from core import fork_cc, fork_cc_io
    from core.fork_types import (
        ProviderForkRequest,
        ProviderForkResult,
)

    db, row, _ = _launched_session(vendors, 'claude-code', tmp_path)
    seen = []
    process = SimpleNamespace(returncode=0)
    async def spawn(argv, *, cwd, env):
        seen.append((argv, cwd, env))
        return process
    async def communicate(proc, request, *args, **kwargs):
        from core.fork_account import require_fork_write

        require_fork_write(request)
        assert proc is process
        return fork_cc_io.CCForkOutput(stdout=b'synthetic', stderr=b'', truncated=False)
    monkeypatch.setattr(fork_cc, '_spawn_process', spawn)
    monkeypatch.setattr(fork_cc_io, 'communicate_fork', communicate)
    monkeypatch.setattr('core.fork_cc_result.result_after_process', lambda *a, **kw: ProviderForkResult('body', None))
    request = CheckpointProviderForkRequest('checkpoint', 'destination', _target('claude-code'), 'instruction', None,
        str(tmp_path), binary='/synthetic/claude', vendor_account_id=row['id'], vendor_account_bound=True)
    result = await fork_cc.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert result.raw_body == 'body' and len(seen) == 1
    assert seen[0][2]['CLAUDE_CONFIG_DIR'] == row['source_home']


@pytest.mark.asyncio
@pytest.mark.parametrize('supersede', [False, True])
async def test_actual_checkpoint_claude_exchange_rechecks_login_after_context_await(vendors, tmp_path, supersede):
    import json
    import time
    from connections.vendor_accounts import set_account_state
    from core.fork_account import admit_fork
    from core.fork_cc_io import communicate_fork
    from core.checkpoint_types import CheckpointProviderForkRequest

    db, row, _ = _launched_session(vendors, 'claude-code', tmp_path)
    request = admit_fork(CheckpointProviderForkRequest('checkpoint', 'destination', _target('claude-code'),
        'instruction', None, str(tmp_path), warm_source=object(), vendor_account_id=row['id'], vendor_account_bound=True))
    frames, lines, ended = [], asyncio.Queue(), asyncio.Event()
    class Process:
        returncode = None
        def __init__(self):
            self.stdin = SimpleNamespace(write=self.write, drain=self.drain, close=self.terminate)
            self.stdout = SimpleNamespace(readline=self.readline)
            self.stderr = SimpleNamespace(read=self.empty)
        def write(self, payload):
            frame = json.loads(payload)
            frames.append(frame)
            if frame['type'] == 'control_request':
                lines.put_nowait(json.dumps({'type':'control_response','response':{
                    'request_id':frame['request_id'], 'subtype':'success',
                    'response':{'model':'test','totalTokens':10,'maxTokens':200000}}}).encode()+b'\n')
            else:
                lines.put_nowait(b'{"type":"result","result":"body"}\n')
        async def drain(self):
            await asyncio.sleep(0)
            if supersede and len(frames) == 1:
                set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
        async def readline(self):
            return await lines.get()
        async def empty(self, _size):
            return b''
        def terminate(self):
            self.returncode = 0
            ended.set()
            lines.put_nowait(b'')
        async def wait(self):
            await ended.wait()
            return self.returncode
    process = Process()
    if supersede:
        with pytest.raises(AccountAdmissionError):
            await communicate_fork(process, request, asyncio.Event(), spawned_at=time.monotonic())
        assert [frame['type'] for frame in frames] == ['control_request']
    else:
        result = await communicate_fork(process, request, asyncio.Event(), spawned_at=time.monotonic())
        assert b'body' in result.stdout
        assert [frame['type'] for frame in frames] == ['control_request', 'user']
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['checkpoint', 'detached'])
@pytest.mark.parametrize('failure', [None, 'login', 'source-gone', 'projection'])
async def test_actual_codex_operation_turn_writer_uses_temporary_own_home_and_login(vendors, tmp_path, operation, failure):
    import json
    from unittest.mock import AsyncMock
    from connections.account_operation import operation_directory
    from connections.vendor_accounts import set_account_state
    from core.fork_account import admit_fork
    from core.fork_codex import _fork_session
    from core.checkpoint_types import CheckpointProviderForkRequest
    from llm.isolated_admission import admit_structured
    from llm.isolated_structured import IsolatedStructuredRequest, _new_codex_invocation_session
    from subagents.codex_app_server import CodexAppServerSession

    db, row, _ = _launched_session(vendors, 'codex', tmp_path)
    target = _target('codex')
    if operation == 'checkpoint':
        request = admit_fork(CheckpointProviderForkRequest('checkpoint', 'destination', target,
            'instruction', None, str(tmp_path), tool_registry={}, dynamic_tool_specs=(),
            vendor_account_id=row['id'], vendor_account_bound=True))
    else:
        request = admit_structured(target, IsolatedStructuredRequest(system_prompt='system',
            user_prompt='user', output_schema={}, max_output_tokens=32, thread_id='destination',
            vendor_account_id=row['id'], vendor_account_bound=True))
    writes, observed, accepted = [], [], []
    async def noop(*args):
        pass
    with operation_directory(request, prefix='corsia-operation-proof-', kind=operation,
            model=target.catalog_id) as (frozen, home):
        if operation == 'checkpoint':
            session = _fork_session(frozen, None, source_home=Path(row['source_home']),
                isolated_home=home, cwd=str(tmp_path), on_event=noop, on_exit=noop, kind=CHECKPOINT_KIND)
        else:
            session = _new_codex_invocation_session(target, frozen,
                SimpleNamespace(on_event=noop, on_exit=noop, on_raw_message=noop),
                binary='/synthetic/codex', working_dir=tmp_path, codex_home=home, session_factory=CodexAppServerSession)
        assert session.codex_home == str(home) != row['runtime_home']
        assert session.source_codex_home == row['source_home']
        assert session.build_env()['CODEX_HOME'] == str(home)
        assert (home / 'auth.json').is_symlink()
        assert (home / 'auth.json').resolve() == Path(row['source_home']) / 'auth.json'
        session.thread_id = 'synthetic-provider-thread'
        if session._tool_requests is not None:
            rollout = home / 'synthetic-rollout.jsonl'
            rollout.write_text('{}\n')
            session._tool_requests.rollout_path = str(rollout)
        def write(payload):
            frame = json.loads(payload)
            writes.append(frame)
            asyncio.get_running_loop().call_soon(session._pending[frame['id']].set_result,
                {'turn': {'id': 'synthetic-provider-turn'}})
        session._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()), returncode=None)
        if failure == 'login':
            set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
        elif failure == 'source-gone':
            (Path(row['source_home']) / 'auth.json').unlink()
        elif failure == 'projection':
            (home / 'auth.json').unlink()
            (home / 'auth.json').write_text('synthetic replacement')
        if failure:
            with pytest.raises(AccountAdmissionError):
                await session.start_turn('user', on_write_observed=lambda: observed.append(True),
                    on_drain_confirmed=lambda: accepted.append(True))
            assert writes == observed == accepted == []
        else:
            assert await session.start_turn('user', on_write_observed=lambda: observed.append(True),
                on_drain_confirmed=lambda: accepted.append(True))
            assert len(writes) == len(observed) == len(accepted) == 1
            assert writes[0]['method'] == 'turn/start'
            assert writes[0]['params']['input'][0]['text'] == 'user'
            assert writes[0]['params']['model'] == target.transport_model
    assert not home.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('supersede', [False, True])
async def test_actual_detached_claude_communicate_rejects_login_changed_in_dispatch(vendors, tmp_path, monkeypatch, supersede):
    import json
    from connections.vendor_accounts import set_account_state
    from connections.account_receipts import capture_receipt
    from connections.account_evidence import mark_discovery
    from llm import isolated_structured

    db, row, _ = _launched_session(vendors, 'claude-code', tmp_path)
    spawned, writes = [], []
    class Process:
        returncode = None
        async def communicate(self, payload=None):
            if self.returncode is not None:  # the reap after a kill
                return b'', b''
            writes.append(payload)
            self.returncode = 0
            return json.dumps({'subtype': 'success', 'num_turns': 1,
                'modelUsage': {'test': {}}, 'structured_output': {'payload': {'value': 'body'}}}).encode(), b''
        def kill(self):
            self.returncode = -9
    process = Process()
    async def spawn(*argv, **kwargs):
        spawned.append((argv, kwargs))
        return process
    async def observe():
        await asyncio.sleep(0)
        if supersede:
            set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
            current = capture_receipt('claude-code', account_id=row['id'], db=db)
            assert mark_discovery(row['id'], receipt=current, status='ready',
                models={'claude-code/test': {'available': True}}, db=db)
            db.commit()
    monkeypatch.setattr(isolated_structured.asyncio, 'create_subprocess_exec', spawn)
    request = isolated_structured.IsolatedStructuredRequest(system_prompt='system', user_prompt='user',
        output_schema={'type': 'object'}, max_output_tokens=32, thread_id='destination',
        vendor_account_id=row['id'], vendor_account_bound=True, dispatch_observer=observe)
    async def invoke():
        return await isolated_structured._invoke_claude_code_structured(_target('claude-code'), request,
            binary='/synthetic/claude', clean_env=lambda: {'PATH': '/synthetic'})
    if supersede:
        with pytest.raises(AccountAdmissionError, match='account_login_unverified'):
            await invoke()
        assert writes == [] and process.returncode == -9
    else:
        assert await invoke() == {'value': 'body'}
        assert writes == [b'user'] and process.returncode == 0
    assert len(spawned) == 1
    assert spawned[0][1]['env']['CLAUDE_CONFIG_DIR'] == row['source_home']
    assert not Path(spawned[0][1]['cwd']).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cc-feed", "turn/start", "turn/steer"])
async def test_session_paid_writer_rechecks_custom_account_before_bytes(vendors, method):
    from connections.account_policy import AccountAdmissionError, select_account
    from subagents.cc_stream import CCStreamSession
    from subagents.codex_app_server import CodexAppServerSession

    db, identities = vendors
    provider = "claude-code" if method == "cc-feed" else "codex"
    account = identities[provider]
    select_account("destination", provider, account_id=account["id"], db=db)
    writes = []
    writer = SimpleNamespace(write=writes.append, drain=AsyncMock())
    if method == "cc-feed":
        session = CCStreamSession(binary="/bin/synthetic", session_id="test", corsia_thread_id="destination")
    else:
        session = CodexAppServerSession(binary="/bin/synthetic", task=None, corsia_thread_id="destination")
    session._proc = SimpleNamespace(stdin=writer, returncode=None)
    observed, accepted = [], []
    with pytest.raises(AccountAdmissionError) as rejected:
        if method == "cc-feed":
            await session.feed("input", on_write_observed=lambda: observed.append(True), on_accepted=lambda: accepted.append(True))
        else:
            await session._request(method, {"input": "input"}, on_write_observed=lambda: observed.append(True),
                                   on_drain_confirmed=lambda: accepted.append(True))
    assert rejected.value.reason == "account_runtime_pending"
    assert writes == observed == accepted == []
    writer.drain.assert_not_awaited()
    if method != "cc-feed":
        assert session._pending == {} and session._pending_methods == {}
        assert session.request_log == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude-code"])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_checkpoint_writer_account_blocks_before_home_or_process(vendors, provider, warm, unknown, monkeypatch):
    from connections.account_policy import AccountAdmissionError
    from vendor_account_fixtures import set_legacy_policy
    from core.checkpoint_types import CheckpointProviderForkRequest
    from core import fork_cc, fork_codex

    db, identities = vendors
    account = identities[provider]
    set_legacy_policy("destination-project", provider, allowlist=[account["id"]], default_account_id=account["id"], db=db)
    # The source model is Codex, including when the actual writer is Claude.
    request = CheckpointProviderForkRequest("checkpoint", "destination", _target(provider), "instruction", None,
                                        "/tmp", binary="/bin/synthetic", warm_source=object() if warm else None,
                                        vendor_account_id=None if unknown else account["id"], vendor_account_bound=True)
    spawn = AsyncMock(side_effect=AssertionError("provider process must not start"))
    codex_run = AsyncMock(side_effect=AssertionError("provider app server must not start"))
    monkeypatch.setattr(fork_cc, "_spawn_process", spawn)
    monkeypatch.setattr(fork_codex, "_run_app_server_fork", codex_run)
    home = []
    monkeypatch.setattr("llm.codex_rollout_files.default_codex_sessions_root", lambda: home.append("ambient"))
    adapter = fork_cc if provider == "claude-code" else fork_codex
    with pytest.raises(AccountAdmissionError) as rejected:
        await adapter.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert rejected.value.reason == ("account_project_denied" if unknown else "account_model_not_discovered")
    spawn.assert_not_awaited()
    codex_run.assert_not_awaited()
    assert home == []
    assert request.vendor_account_id == (None if unknown else account["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude-code"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_detached_structured_writer_blocks_before_ambient_fallback(vendors, provider, unknown, monkeypatch):
    from connections.account_policy import AccountAdmissionError
    from vendor_account_fixtures import set_legacy_policy
    from llm import isolated_structured

    db, identities = vendors
    account = identities[provider]
    set_legacy_policy("destination-project", provider, allowlist=[account["id"]], default_account_id=account["id"], db=db)
    spawn = AsyncMock(side_effect=AssertionError("provider process must not start"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    factory = []
    monkeypatch.setattr(isolated_structured, "_new_codex_invocation_session", lambda *a, **k: factory.append(k))
    with pytest.raises(AccountAdmissionError) as rejected:
        await isolated_structured.invoke_isolated_structured(
            _target(provider), system_prompt="system", user_prompt="user", output_schema={}, validator=lambda x: x,
            max_output_tokens=32, binary="/bin/synthetic", thread_id="destination",
            account_id=None if unknown else account["id"], account_bound=True)
    assert rejected.value.reason == ("account_project_denied" if unknown else "account_model_not_discovered")
    spawn.assert_not_awaited()
    assert factory == []


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery', ['zero', 'ambiguous', 'accepted'])
async def test_codex_link_change_requeues_unwritten_input_and_never_resends_written_input(vendors, tmp_path, delivery):
    from connections.account_policy import AccountAdmissionError
    from connections.account_projection import validate_projection, cleanup_stale
    from connections.account_receipts import capture_receipt
    from connections.account_evidence import mark_discovery
    from connections.account_holds import refresh_thread_holds
    from connections.vendor_accounts import set_account_state
    from infra.database import write_transaction
    from infra.input_writes import dispatch_fenced_batch
    from infra.llm_usage import record_llm_usage
    from infra.thread_driver import finish_turn
    from infra.thread_inputs import start_idle_turn
    from subagents.vendor_write_admission import prepare_session_launch

    db, row, session = _launched_session(vendors, 'codex', tmp_path)
    batch = _frozen_batch(db)
    prepare_session_launch(session, 'codex')
    _accept(db, 'later')
    record_llm_usage('destination', 'codex/test', {'prompt_tokens': 5, 'completion_tokens': 7, 'total_tokens': 12},
                     turn_id='frozen', usage_key='prior-delivered-call', _emit_product_event=lambda *args, **kwargs: None)
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at) VALUES ('output','destination','assistant','partial output','t')")
    db.commit()
    factual_before = [tuple(item) for item in db.execute("SELECT vendor_account_id,total_tokens FROM llm_usage WHERE turn_id='frozen'")]
    assert factual_before == [(row['id'], 12)]
    projected = Path(session.codex_home) / 'auth.json'
    writes = []

    def replace_projection():
        projected.unlink()
        projected.write_text('synthetic vendor replacement')
        try:
            validate_projection(session.vendor_account_receipt, home=Path(session.codex_home))
        except AccountAdmissionError as error:
            assert error.reason == 'codex_auth_projection_changed'
            return error
        raise AssertionError('replacement must refuse')

    def write(data):
        writes.append(data)
        if delivery == 'accepted':
            error = replace_projection()
            identity = json.loads(data)['id']
            asyncio.get_running_loop().call_soon(session._pending[identity].set_exception, error)

    async def drain():
        if delivery == 'ambiguous':
            raise replace_projection()

    session._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=drain), returncode=None)

    async def paid(evidence):
        observed = []
        if delivery == 'zero':
            replace_projection()  # Race occurs after the canonical write fence.
        try:
            return await session._request('turn/start', {'input': 'input'},
                on_write_observed=lambda: observed.append(True), on_drain_confirmed=evidence.mark_accepted)
        except BaseException:
            if observed and not evidence.accepted:
                evidence.mark_ambiguous()
            raise

    with pytest.raises(AccountAdmissionError, match='codex_auth_projection_changed'):
        await dispatch_fenced_batch(batch, paid, attempt_id='c6-write', started_at='write')
    current = dict(db.execute("SELECT * FROM messages WHERE id='input'").fetchone())
    expected = {'zero': ('queued', 'proved_no_write'), 'ambiguous': ('ambiguous', 'ambiguous'), 'accepted': ('delivered', 'accepted')}[delivery]
    assert (current['delivery_state'], current['write_outcome']) == expected
    assert len(writes) == (0 if delivery == 'zero' else 1)
    if delivery == 'zero':
        assert current['active_turn_id'] is None
        assert current['delivery_error'] == 'codex_auth_projection_changed'
        assert current['delivery_attempt_id'] is current['write_started_at'] is None
        assert current['seq'] is None
    settlement = finish_turn('frozen', outcome='error', ended_at='error', successor_turn_id='refused-successor',
                             boot_epoch='test', error='codex_auth_projection_changed', db=db)
    assert settlement.successor is None
    assert db.execute("SELECT status,error,vendor_account_id FROM active_turns WHERE id='frozen'").fetchone()[:] == ('error', 'codex_auth_projection_changed', row['id'])
    assert db.execute("SELECT content FROM messages WHERE id='output'").fetchone()[0] == 'partial output'
    assert [tuple(item) for item in db.execute("SELECT vendor_account_id,total_tokens FROM llm_usage WHERE turn_id='frozen'")] == factual_before
    if delivery != 'zero':
        assert dict(db.execute("SELECT * FROM messages WHERE id='input'").fetchone()) == current
    session._proc = None  # Synthetic transport has quiesced; no process exists.
    account = set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert cleanup_stale(receipt, quiesced=True, db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={'codex/test': {'available': True}}, db=db)
    db.commit()
    with write_transaction(db, scope='test_link_change_recovery'):
        refresh_thread_holds('destination', wake=False, db=db)
    successor = start_idle_turn('destination', turn_id='fresh-login-successor', started_at='ready', boot_epoch='test', db=db)
    assert successor is not None and successor.vendor_receipt == receipt
    assert [item.message_id for item in successor.items] == (['input', 'later'] if delivery == 'zero' else ['later'])
    assert len(writes) == (0 if delivery == 'zero' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
async def test_actual_runtime_stop_cancels_turn_after_concrete_home_failure(vendors, tmp_path, monkeypatch, provider):
    from core.turn_core import persist_turn_start
    from host_access.processes import PROCESS_OWNER
    from subagents import cc_runtime, codex_runtime
    from subagents.cc_runtime_state import _Live
    from subagents.codex_runtime_state import _LiveCodex

    db, row, session = _launched_session(vendors, provider, tmp_path)
    prepare_session_launch(session, provider)
    identity = persist_turn_start('frozen', 'destination', provider + '/test', 'input', db=db)
    db.commit()
    writes, stops = [], []
    process = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock(),
        is_closing=lambda: True), returncode=None, stderr=None, wait=AsyncMock(return_value=0))
    session._proc = process
    if provider == 'codex':
        runtime = codex_runtime
        projected = Path(session.codex_home) / 'auth.json'
        projected.unlink()
        projected.write_text('synthetic vendor replacement')
        live = _LiveCodex(session=session, parent_thread_id=None, name='synthetic', corsia_turn=identity,
            catalog_model_id='codex/test')
        with pytest.raises(AccountAdmissionError, match='codex_auth_projection_changed'):
            await session._request('turn/start', {'input': 'must not write'})
    else:
        runtime = cc_runtime
        Path(session.claude_dir).rmdir()
        live = _Live(session=session, parent_thread_id=None, name='synthetic', corsia_turn=identity,
            turn_active=True, model='test', catalog_model_id='claude-code/test')
        with pytest.raises(AccountAdmissionError, match='account_home_mismatch'):
            await session.feed('must not write')
    assert writes == []
    def halt():
        stops.append('interrupt')
    async def terminate():
        stops.append('joined')
        process.returncode = 0
    monkeypatch.setattr(session, '_request_terminate', halt)
    monkeypatch.setattr(session, 'terminate', terminate)
    monkeypatch.setattr(PROCESS_OWNER, 'terminate_turn', AsyncMock())
    runtime._LIVE.reset()
    runtime._LIVE.put('destination', live)
    try:
        assert await runtime.stop('destination', expected_turn_id='frozen', broadcast=lambda *_args: None)
        assert stops[0] == 'interrupt' and 'joined' in stops
        assert runtime._LIVE.get('destination') is None
        terminal = db.execute("SELECT status,vendor_account_id FROM active_turns WHERE id='frozen'").fetchone()
        assert tuple(terminal) == ('cancelled', row['id'])
        delivery = db.execute("SELECT delivery_state,write_outcome FROM messages WHERE id='input'").fetchone()
        assert tuple(delivery) == ('cancelled', 'proved_no_write')
        assert writes == []
    finally:
        runtime._LIVE.reset()


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
@pytest.mark.parametrize('destination,source', [('default','custom'), ('custom','default'), ('x','y')])
def test_cross_account_warm_home_refuses_before_copy(tmp_path, provider, destination, source):
    expected = str(tmp_path / destination)
    custom = destination != 'default'
    launch = LaunchHomes(None if not custom else destination, custom,
        claude_dir=expected if provider == 'claude-code' and custom else None,
        # An added Codex account logs in on ``codex_source`` and transcripts on
        # ``codex_runtime``, and ``homes_for`` refuses them equal: only the
        # transcript home can hold its warm rollout.
        codex_source=str(tmp_path / destination / 'login') if provider == 'codex' and custom else None,
        codex_runtime=expected if provider == 'codex' and custom else None,
        warm_home=expected if not custom else None)
    request = SimpleNamespace(warm_source=object(), vendor_launch=launch)
    with pytest.raises(WarmForkUnusable, match='warm_fork_home_mismatch'):
        require_same_operation_home(request, str(tmp_path / source))
    require_same_operation_home(request, expected)


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_added_account_warm_home_is_where_its_own_transcript_lives(vendors, tmp_path, provider):
    """A real added account's warm home is its runtime tree, never another account's or a login tree.

    Production observed idle warm checkpoints refused on an added account while
    cold runs on the same account succeeded. The rollout root comes from
    ``account_sessions_root`` (the account's runtime home for Codex), so the
    guard has to accept that home and still refuse a foreign one.
    """
    from core.fork_account import admit_fork
    from core.checkpoint_types import CheckpointProviderForkRequest

    db, row, _ = _launched_session(vendors, provider, tmp_path)
    receipt = capture_receipt(provider, account_id=row['id'], db=db)
    assert receipt is not None
    request = admit_fork(CheckpointProviderForkRequest('checkpoint', 'destination', _target(provider),
        'instruction', None, str(tmp_path), warm_source=SimpleNamespace(vendor_account_receipt=receipt),
        vendor_account_id=row['id'], vendor_account_bound=True))
    assert request.vendor_launch.custom and request.vendor_account_receipt == receipt
    # Claude has one explicit config home; Codex transcripts under its runtime home.
    transcript = row['source_home'] if provider == 'claude-code' else row['runtime_home']
    foreign = row['runtime_home'] if provider == 'claude-code' else row['source_home']
    assert Path(transcript).absolute() != Path(foreign).absolute()
    require_same_operation_home(request, transcript)
    # A home that is not where this account's transcripts live is refused.
    with pytest.raises(WarmForkUnusable, match='warm_fork_home_mismatch'):
        require_same_operation_home(request, foreign)
    with pytest.raises(WarmForkUnusable, match='warm_fork_home_mismatch'):
        require_same_operation_home(request, str(tmp_path / 'another-account'))
    # Another account's transcript home at the same path shape is refused too.
    with pytest.raises(WarmForkUnusable, match='warm_fork_home_mismatch'):
        require_same_operation_home(request, str(Path(transcript).parent / 'not-this-account' / 'sessions'))
    # The receipt fence still binds the accepted home to the login it ran on.
    stale = replace(request, vendor_account_receipt=replace(receipt, login_seq=receipt.login_seq + 1))
    with pytest.raises(WarmForkUnusable, match='warm_fork_login_mismatch'):
        require_same_operation_home(stale, transcript)


@pytest.mark.asyncio
async def test_added_account_warm_checkpoint_reaches_a_disposable_launch(vendors, tmp_path, monkeypatch):
    """The adapter forks an added account's warm rollout into a disposable home instead of refusing it.

    Drives capture through the real registry and rollout, then the actual
    ``invoke_fork`` up to the checkpoint launch, with a fake provider that
    never spawns a process and never reaches inference.
    """
    from connections.vendor_accounts import get_account
    from core import fork_codex as adapter
    from core.fork_account import admit_fork
    from core.fork_types import (
        ProviderForkRequest,
        ProviderForkRejected,
)
    from history.types import HistoryBoundary
    from infra.vendor_login import stamp_initial_claim
    from registry.service import register_codex_thread

    db, row, _ = _launched_session(vendors, 'codex', tmp_path)
    provider_id = '11111111-1111-4111-8111-111111111111'
    sessions = Path(row['runtime_home']) / 'sessions'
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f'rollout-{provider_id}.jsonl'
    body = json.dumps({'type': 'session_meta', 'payload': {'id': provider_id, 'cwd': str(tmp_path)}}) + '\n'
    rollout.write_text(body)
    # The Turn that wrote the transcript, stamped with the login it ran on.
    assert stamp_initial_claim(db, 'frozen', 'input') is not None
    db.commit()
    register_codex_thread('destination', provider_id, rollout_path=str(rollout),
                          reasoning_effort='medium', db=db)
    db.commit()
    source = await adapter.capture_warm_source('destination', HistoryBoundary(
        thread_id='destination', history_epoch=0, seq=1, message_id='synthetic'),
        target=_target('codex'), binary='/synthetic/codex')
    assert source is not None, 'the account root resolution itself already finds the rollout'
    assert Path(source.sessions_root) == sessions
    request = admit_fork(CheckpointProviderForkRequest('checkpoint', 'destination', _target('codex'),
        'instruction', None, str(tmp_path), binary='/synthetic/codex', warm_source=source,
        tool_registry={}, dynamic_tool_specs=(), vendor_account_id=row['id'], vendor_account_bound=True))

    facts, real = {}, adapter._fork_session

    def disposable_launch(frozen, src, *, source_home, isolated_home, cwd, **kwargs):
        """Record what the actual checkpoint launch sees, then refuse to spawn anything."""
        session = real(frozen, src, source_home=source_home, isolated_home=isolated_home, cwd=cwd, **kwargs)
        auth = isolated_home / 'auth.json'
        facts.update(
            pre_isolation=str(source_home), isolated=str(isolated_home),
            codex_home=session.codex_home, login_home=session.source_codex_home,
            copies=sorted(str(p.relative_to(isolated_home)) for p in (isolated_home / 'sessions').rglob('*.jsonl')),
            copied=[p.read_text() for p in (isolated_home / 'sessions').rglob('*.jsonl')],
            auth_symlink=auth.is_symlink(), auth_target=str(auth.resolve()) if auth.exists() else None,
            account_id=frozen.vendor_account_id)
        async def never_spawn():
            return False
        session.start = never_spawn
        return session

    monkeypatch.setattr(adapter, '_fork_session', disposable_launch)
    with pytest.raises(ProviderForkRejected):
        await adapter.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert facts, 'the adapter never reached a disposable checkpoint launch'
    assert facts['pre_isolation'] == row['runtime_home']  # the rollout's own home, not the login tree
    assert facts['account_id'] == get_account(row['id'], db=db)['id']
    assert Path(facts['isolated']) != Path(row['runtime_home'])
    assert facts['codex_home'] == facts['isolated']  # the launch runs in the disposable home
    assert facts['login_home'] == row['source_home']  # auth is still read from the login home
    assert facts['auth_symlink'] and facts['auth_target'] == str(Path(row['source_home']) / 'auth.json')
    assert len(facts['copies']) == 1 and facts['copied'] == [body]
    assert rollout.read_text() == body  # the durable transcript was copied, never rewritten
    assert not Path(facts['isolated']).exists()  # retired with the operation


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_a_warm_source_stays_warm_only_on_the_login_its_thread_last_ran_on(isolated_db, provider):
    from infra.database import get_db
    from infra.vendor_login import thread_login
    from native_checkpoint_fixtures import seed_vendor_checkpoint_thread
    from connections.account_runtime import homes_for
    from vendor_account_fixtures import default_receipt, seed_turn_on_default, signed_in_default

    seed_vendor_checkpoint_thread([_target(provider)])
    signed_in_default(provider, models=(provider + '/test',))
    written = seed_turn_on_default('thread-1', provider + '/test')
    other = 'claude-code' if provider == 'codex' else 'codex'
    db = get_db()
    try:
        assert written is not None and thread_login('thread-1', provider, db=db) == written
        assert thread_login('thread-1', other, db=db) is None
    finally:
        db.close()
    db = get_db()
    try:
        launch = homes_for(get_account(written.account_id, db=db))
    finally:
        db.close()
    home = launch.claude_dir or launch.codex_runtime
    request = SimpleNamespace(warm_source=SimpleNamespace(vendor_account_receipt=written), vendor_launch=launch,
                              vendor_account_receipt=default_receipt(provider))
    require_same_operation_home(request, home)
    request.vendor_account_receipt = replace(written, login_seq=written.login_seq + 1)  # the account signed in again
    with pytest.raises(WarmForkUnusable, match='warm_fork_login_mismatch'):
        require_same_operation_home(request, home)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
async def test_registered_old_login_source_refuses_new_default_before_copy_or_spawn(vendors, tmp_path, monkeypatch, provider):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from vendor_account_fixtures import signed_in_default
    from core.fork_account import admit_fork
    from core.fork_types import (
        ProviderForkRequest,
        WarmForkUnusable,
)
    from history.types import HistoryBoundary
    from registry.models import register_artifact

    db, _ = vendors
    default = signed_in_default(provider, db=db)
    identity = default['id']
    home = Path(default['source_home'] if provider == 'claude-code' else default['runtime_home'])
    provider_id = '11111111-1111-4111-8111-111111111111'
    model = provider + '/test'
    if provider == 'claude-code':
        from core import fork_cc as adapter
        from subagents.cc_invocation import ClaudeInvocation, claude_invocation_metadata

        path = home / 'projects' / 'synthetic' / (provider_id + '.jsonl')
        seed, mcp = tmp_path / 'seed.txt', tmp_path / 'mcp.json'
        seed.write_text('seed')
        mcp.write_text('{"mcpServers":{}}')
        invocation = ClaudeInvocation(binary='/synthetic/claude', permission_mode='bypassPermissions', session_id=provider_id,
            model='test', seed_file=str(seed), mcp_config=str(mcp), working_dir=str(tmp_path))
        metadata = {'claude_invocation': claude_invocation_metadata(invocation)}
        artifact_type = 'session'
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
            "VALUES ('claude-code/test','Test','claude-code/test','claude-code','test','claude-code','t','t')")
    else:
        from core import fork_codex as adapter

        path = home / 'sessions' / ('rollout-' + provider_id + '.jsonl')
        metadata = {'rollout_path': str(path), 'reasoning_effort': ''}
        artifact_type = 'thread'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': provider_id, 'cwd': str(tmp_path)}}) + '\n')
    db.execute("UPDATE threads SET model=? WHERE id='destination'", (model,))
    db.commit()
    def observe(_label):
        set_account_state(identity, 'signed_in', expected_revision=get_account(identity, db=db)['revision'], db=db)
        db.commit()
    old = capture_receipt(provider, account_id=identity, db=db)
    register_artifact(integration=provider, type=artifact_type, external_id=provider_id,
        parent_thread_id='destination', path=str(path), metadata=metadata, db=db)
    from vendor_account_fixtures import seed_turn_on_default
    db.commit()
    assert seed_turn_on_default('destination', model) == old  # the transcript was written under login A
    # The registry and transcript owners find A's durable source while A is current.
    source = await adapter.capture_warm_source('destination', HistoryBoundary(
        thread_id='destination', history_epoch=0, seq=1, message_id='synthetic'),
        target=_target(provider), binary='/synthetic/vendor')
    assert source is not None
    observe('B')
    current = capture_receipt(provider, account_id=identity, db=db)
    assert current.login_seq > old.login_seq
    assert mark_discovery(identity, receipt=current, status='ready', models={model: {'available': True}}, db=db)
    db.commit()
    request = admit_fork(CheckpointProviderForkRequest('checkpoint', 'destination', _target(provider),
        'instruction', None, str(tmp_path), binary='/synthetic/vendor', warm_source=source))
    assert request.vendor_account_receipt == current
    effects = []
    def forbidden(*args, **kwargs):
        effects.append(True)
        raise AssertionError('unproved warm source reached copy or spawn')
    monkeypatch.setattr(adapter, '_snapshot' if provider == 'claude-code' else '_copy_rollout', forbidden)
    monkeypatch.setattr(adapter, '_spawn_process' if provider == 'claude-code' else '_fork_session', forbidden)
    with pytest.raises(WarmForkUnusable, match='warm_fork_login_mismatch'):
        await adapter.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert effects == []
    assert path.read_text().count('session_meta') == 1


def test_initial_claim_stamp_is_private_and_retry_remains_identical(accounts):
    from infra.vendor_login import read_turn_receipt
    from infra.send_broadcast import project_input_message, read_input_broadcast_row

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "m", {"visible": "kept"})
    batch = _start(db, "T1")
    receipt = read_turn_receipt("T1", db=db)
    assert receipt is not None
    assert receipt.account_id == default["id"]
    from connections.account_receipts import generation_revision
    from connections.vendor_accounts import get_account

    revision = generation_revision(get_account(default["id"], db=db))  # the Default was signed in, so not row revision 0
    assert (receipt.account_revision, receipt.login_seq) == (revision, 1)
    assert batch.vendor_receipt == receipt
    assert batch.items[0].metadata == {"visible": "kept"}
    assert json.loads(db.execute("SELECT metadata FROM messages WHERE id='m'").fetchone()[0])[KEY] == {
        "turn_id": "T1", "account_revision": revision, "login_seq": 1}
    assert _accept(db, "m", {"visible": "kept"}).duplicate
    public = project_input_message(read_input_broadcast_row("m", db=db))
    assert KEY not in public.get("metadata", {})
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id='m'")
    assert read_turn_receipt("T1", db=db) == receipt


def test_same_message_successor_cannot_supply_old_turn_login(accounts):
    from infra.input_writes import release_batch_before_write
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "m")
    first = _start(db, "T1")
    release_batch_before_write(first, db=db)
    assert read_turn_receipt("T1", db=db) is None
    assert KEY not in json.loads(db.execute("SELECT metadata FROM messages WHERE id='m'").fetchone()[0] or "{}")
    db.execute("UPDATE active_turns SET status='completed' WHERE id='T1'")
    db.commit()
    _known_default(db, default, login=2)
    second = _start(db, "T2")
    assert second.vendor_receipt.login_seq == 2
    assert read_turn_receipt("T1", db=db) is None
    assert read_turn_receipt("T2", db=db) == second.vendor_receipt


def test_boundary_keeps_initial_login_and_never_mints_new_anchor(accounts):
    from infra.thread_inputs import claim_safe_boundary
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "initial")
    first = _start(db, "T1")
    _known_default(db, default, login=2)
    _accept(db, "boundary")
    boundary = claim_safe_boundary("destination", "T1", db=db)
    assert boundary.vendor_receipt == first.vendor_receipt
    assert KEY not in json.loads(db.execute("SELECT metadata FROM messages WHERE id='boundary'").fetchone()[0] or "{}")
    db.execute("UPDATE messages SET active_turn_id=NULL WHERE id='initial'")
    assert read_turn_receipt("T1", db=db) is None


@pytest.mark.parametrize("value", [None, "null", '"scalar"', "7", "true", "[]", "invalid"])
@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
def test_detach_expression_preserves_nonobject_metadata_during_terminal(accounts, value, outcome):
    from infra.input_writes import settle_unwritten_turn_inputs
    from infra.vendor_login import without_login_sql

    db, default, _, _ = accounts
    _accept(db, "m")
    _start(db, "T1")
    db.execute("UPDATE messages SET metadata=? WHERE id='m'", (value,))
    # Every detach owner uses this guarded expression; it is safe even on legacy malformed data.
    db.execute(f"UPDATE messages SET metadata={without_login_sql()} WHERE id='m'")
    assert db.execute("SELECT metadata FROM messages WHERE id='m'").fetchone()[0] == value
    settle_unwritten_turn_inputs(db, "T1", outcome=outcome, error=None)
    assert db.execute("SELECT metadata FROM messages WHERE id='m'").fetchone()[0] == value


@pytest.mark.parametrize("stamp", [None, {}, {"turn_id": "other", "account_revision": 0, "login_seq": 1},
    {"turn_id": "T1", "account_revision": True, "login_seq": 1},
    {"turn_id": "T1", "account_revision": 0, "login_seq": -1},
    {"turn_id": "T1", "account_revision": 0, "login_seq": 1, "extra": 2}])
def test_raw_reader_refuses_missing_malformed_or_foreign_stamp(accounts, stamp):
    from infra.vendor_login import read_turn_receipt

    db, _, _, _ = accounts
    _accept(db, "m")
    _start(db, "T1")
    db.execute("UPDATE messages SET metadata=? WHERE id='m'", (json.dumps({KEY: stamp}),))
    assert read_turn_receipt("T1", db=db) is None


@pytest.mark.parametrize("retry", [False, True])
def test_reserved_stamp_is_rejected_before_duplicate_with_no_value_disclosure(accounts, retry):
    db, _, _, _ = accounts
    if retry:
        _accept(db, "m")
    with pytest.raises(ValueError, match="^reserved input metadata: _corsia_vendor_login$"):
        _accept(db, "m", {KEY: {"secret-shaped-value": "must-never-be-echoed"}})
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id='m'").fetchone()[0] == int(retry)


@pytest.mark.parametrize("owner", ["account", "before", "before-held", "zero", "completed", "checkpoint-held", "checkpoint-release"])
def test_every_canonical_detach_removes_only_login_stamp(accounts, owner):
    from connections.account_policy import AccountAdmissionError
    from infra.input_writes import (begin_batch_write, release_batch_after_proved_no_write,
        release_batch_before_write, settle_account_refusal, settle_unwritten_turn_inputs)
    from infra.checkpoint_refusal_settlement import _hold, _release
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "m", {"visible": {"exact": [1, False, None]}})
    batch = _start(db, "T1")
    rows = db.execute("SELECT * FROM messages WHERE id='m'").fetchall()
    if owner == "account":
        settle_account_refusal(db, rows, AccountAdmissionError("account_signed_out"))
    elif owner.startswith("before"):
        release_batch_before_write(batch, hold_error="account_signed_out" if owner == "before-held" else None, db=db)
    elif owner == "zero":
        begin_batch_write(batch, attempt_id="attempt", started_at="t", db=db)
        release_batch_after_proved_no_write(batch, attempt_id="attempt", error="zero", db=db)
    elif owner == "completed":
        settle_unwritten_turn_inputs(db, "T1", outcome="completed", error=None)
    elif owner == "checkpoint-held":
        _hold(db, rows, "checkpoint-error")
    else:
        _release(db, rows)
    row = db.execute("SELECT active_turn_id,metadata FROM messages WHERE id='m'").fetchone()
    assert row['active_turn_id'] is None
    assert json.loads(row['metadata']) == {"visible": {"exact": [1, False, None]}}
    assert read_turn_receipt("T1", db=db) is None


def test_stamp_claim_and_detach_are_in_original_transaction(accounts):
    from infra.database import write_transaction
    from infra.input_writes import release_batch_before_write
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "m")
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_initial_rollback"):
            _start(db, "rolled")
            raise RuntimeError("rollback")
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE id='rolled'").fetchone()[0] == 0
    assert db.execute("SELECT metadata FROM messages WHERE id='m'").fetchone()[0] is None
    batch = _start(db, "T1")
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_detach_rollback"):
            release_batch_before_write(batch, db=db)
            raise RuntimeError("rollback")
    assert read_turn_receipt("T1", db=db) == batch.vendor_receipt


def test_stale_same_account_receipt_cannot_borrow_new_login(accounts):
    from connections.account_receipts import receipt_is_current

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, "m")
    batch = _start(db, "T1")
    assert receipt_is_current(batch.vendor_receipt, db=db)
    _known_default(db, default, login=2)
    assert not receipt_is_current(batch.vendor_receipt, db=db)
    assert not receipt_is_current(None, db=db)
