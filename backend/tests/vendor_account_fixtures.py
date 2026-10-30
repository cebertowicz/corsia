"""Name a provider's accounts explicitly in tests: the Default is a row like any other.

The one shared support module for the account suites: a fixture or helper that more than
one account module uses lives here, so no account module imports one out of a sibling
account module or out of a retained re-export path."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from connections import account_moves, account_rollover, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt, ReceiptFence
from connections.usage_verdicts import LimitSubject
from connections.provider_policy import effective_policy, set_policy
from connections.vendor_accounts import list_accounts, resolve_account_id
from infra import database
from tests.child_parent_notice_helpers import delegated


KEY = "_corsia_vendor_login"


THREAD, MODEL = "claude-destination", "claude-code/test"


class CompetingRead:
    def __init__(self, db, competitor, account_id):
        self.db, self.competitor, self.account_id = db, competitor, account_id
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        result = self.db.execute(sql, parameters)
        if not self.fired and "AS why FROM threads" in sql or (
                not self.fired and 'AS child_plan_state' in sql):
            self.fired = True
            self.competitor.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (self.account_id,))
            self.competitor.commit()
        return result


def _read(fn, db):
    connection = db or database.get_db()  # read at call time so a patched get_db is honoured
    try:
        return fn(connection)
    finally:
        if db is None:
            connection.close()


def seed_default_account(provider: str, *, db=None) -> dict:
    """Create a home-backed Default for tests that do not exercise sign-in."""
    from connections.vendor_accounts import add_account, get_account

    def write(connection):
        account = add_account(provider, 'Default', db=connection)
        connection.execute('UPDATE vendor_accounts SET is_default=1 WHERE id=?', (account['id'],))
        connection.commit()
        return get_account(account['id'], db=connection)

    return _read(write, db)


def default_id(provider: str, *, db=None) -> str:
    """The provider's Default account row."""
    identity = _read(lambda connection: resolve_account_id(provider, db=connection), db)
    assert identity is not None, f'{provider} has no Default account'
    return identity


def set_legacy_policy(project, provider, *, allowlist=None, default_account_id=None,
                      switch_on_limit=None, db=None):
    """Test-only translation for fixtures that still describe the retired policy shape."""
    def write(connection):
        accounts = list_accounts(provider=provider, db=connection)
        allowed = set(allowlist) if allowlist is not None else {account["id"] for account in accounts}
        order = None if default_account_id is None else [default_account_id, *(allowlist or [])]
        return set_policy(
            project, provider,
            rollover=bool(switch_on_limit),
            order=list(dict.fromkeys(order)) if order is not None else None,
            disabled=[account["id"] for account in accounts if account["id"] not in allowed],
            expected_revision=effective_policy(project, provider, db=connection)["revision"],
            db=connection,
        )

    return _read(write, db)


def model_default_id(model: str, *, db=None) -> str | None:
    """The Default account of the harness that runs ``model`` (``None`` for a non-harness model)."""
    from llm.model_status import harness_connection_for_model

    def read(connection):
        provider = harness_connection_for_model(model, db=connection)
        return resolve_account_id(provider, db=connection) if provider is not None else None

    return _read(read, db)


def receipt_of(provider: str, account_id: str, *, db=None) -> ReceiptFence | None:
    """One account's current login, captured now."""
    return _read(lambda connection: capture_receipt(provider, account_id=account_id, db=connection), db)



def default_receipt(provider: str, *, db=None) -> ReceiptFence | None:
    """The Default account's current login (``None`` when there is no Default row)."""
    def read(connection):
        identity = resolve_account_id(provider, db=connection)
        return capture_receipt(provider, account_id=identity, db=connection) if identity is not None else None

    return _read(read, db)


def default_subject(provider: str, scope: str, *, db=None) -> LimitSubject:
    """One scope of the Default account's current login."""
    receipt = default_receipt(provider, db=db)
    assert receipt is not None, f'{provider} Default has no current login'
    return LimitSubject(provider, scope, receipt.account_id, receipt)


def seed_usage(account_id: str, *, facts: dict, db=None) -> None:
    """Store usage on an account with no current login, as an earlier login left it.

    Fenced writes refuse an account with no current login, so a test that needs
    an earlier login's usage in place writes the stored blob directly.
    """
    import json

    from connections.usage_store import empty_usage

    def write(connection):
        connection.execute('UPDATE vendor_accounts SET usage_json=? WHERE id=?',
                           (json.dumps({**empty_usage(), 'facts': facts}, sort_keys=True), account_id))
        connection.commit()

    _read(write, db)


def seed_turn_on_default(thread_id: str, model: str, *, turn_id: str = 'prior-turn') -> ReceiptFence | None:
    """A finished Turn of ``thread_id`` frozen on its harness's Default: the login its transcript was written under."""
    db = database.get_db()
    try:
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
                   "VALUES (?,?,0,?,'completed','t','b')", (turn_id, thread_id, model))
        return freeze_turn_on_default(db, turn_id)
    finally:
        db.close()


def freeze_turn_on_default(db, turn_id: str) -> ReceiptFence | None:
    """Freeze a hand-seeded Turn on its harness's Default and stamp the login, as a claim does."""
    from infra.vendor_login import stamp_initial_claim
    from llm.model_status import harness_connection_for_model

    turn = db.execute('SELECT thread_id,model FROM active_turns WHERE id=?', (turn_id,)).fetchone()
    provider = harness_connection_for_model(turn['model'], db=db)
    assert provider is not None, f'{turn["model"]} runs on no harness'
    db.execute('UPDATE active_turns SET vendor_account_id=? WHERE id=?', (default_id(provider, db=db), turn_id))
    message_id = f'{turn_id}-input'
    db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,seq,active_turn_id) "
               "VALUES (?,?,'user','input','t',0,?)", (message_id, turn['thread_id'], turn_id))
    receipt = stamp_initial_claim(db, turn_id, message_id)
    db.commit()
    return receipt


def _ready(db, account, model='codex/test'):
    from connections.account_receipts import capture_receipt
    from connections.account_evidence import mark_discovery

    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert receipt is not None
    assert mark_discovery(account['id'], receipt=receipt, status='ready',
                          models={model: {'resolved_identity': 'synthetic'}}, db=db)
    db.commit()
    return receipt


def _known_default(db, account, login=1):
    from connections.account_evidence import invalidate_discovery, mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account

    db.execute("UPDATE vendor_accounts SET state='signed_in', identity_json=? WHERE id=?",
               (json.dumps({'account_fp': 'synthetic'}), account['id']))
    current = get_account(account['id'], db=db)
    (Path(current['source_home']) / 'auth.json').touch()
    invalidate_discovery(current, login_seq=login, auth_state='known', db=db)
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={'codex/test': {}}, db=db)
    db.commit()


def _accept(db, identity, metadata=None):
    from infra.input_admission import accept_thread_input

    return accept_thread_input("destination", role="user", content=identity, intent="apply_now",
                               message_id=identity, metadata=metadata, db=db)


def _start(db, turn):
    from infra.thread_inputs import start_idle_turn

    return start_idle_turn("destination", turn_id=turn, boot_epoch="test", started_at="t", db=db)


def _row(db, identity='m'):
    return dict(db.execute('SELECT active_turn_id,delivery_state,delivery_error,write_outcome,delivery_revision,metadata FROM messages WHERE id=?', (identity,)).fetchone())


def _held(db, identity='m', cause='account_model_not_discovered'):
    db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error=? WHERE id=?", (cause, identity))
    db.commit()


def _kind(db):
    from infra.unavailable_model_recovery import held_head_kind_sql, held_head_sql
    from plans.wait_causes import stalled_why

    row = db.execute(f"SELECT {held_head_kind_sql(':thread')} AS kind, {held_head_sql(':thread')} AS held", {'thread': 'destination'}).fetchone()
    assert bool(row['held']) == (row['kind'] is not None)
    return row['kind'], stalled_why(db, 'destination')


def queue(db, identity='brief'):
    from infra.input_admission import accept_thread_input
    return accept_thread_input('worker', role='user', content='Keep this exact brief',
                               intent='apply_now', message_id=identity, db=db)


def ready(db, account):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from infra.database import write_transaction
    with write_transaction(db, scope='test_account_readiness'):
        receipt = capture_receipt('codex', account_id=account['id'], db=db)
        assert receipt is not None
        assert mark_discovery(account['id'], receipt=receipt, status='ready',
                              models={'codex/test': {'available': True}}, db=db)


def _configured(accounts, monkeypatch, *, enabled=True):
    db, _, first, second = accounts
    for row in (first, second):
        receipt = capture_receipt('codex', account_id=row['id'], db=db)
        mark_discovery(row['id'], receipt=receipt, status='ready',
            models={'codex/test': {'available': True, 'resolved_identity': 'synthetic'}}, db=db)
    db.commit()
    set_legacy_policy('destination-project', 'codex', allowlist=[first['id'], second['id']],
                      default_account_id=first['id'], switch_on_limit=enabled, db=db)
    select_account('destination', 'codex', db=db)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', first['id'], db=db), reason='usage_limit',
        resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update', db=db)
    harness_usage.record_allowed(provider='codex', receipt=receipt_of('codex', second['id'], db=db), source='discovery', db=db)
    events = []
    monkeypatch.setattr(account_moves, '_publish', events.append)
    return db, first, second, events


def _move(db):
    from infra.thread_inputs import _claimable_prefix

    prefix = _claimable_prefix(db, 'destination')
    if not prefix:
        return None
    return account_rollover.commit_rollover(
        account_rollover.plan_rollover('destination', eligible_rows=prefix[2], db=db), db=db)


@pytest.fixture
def accounts(isolated_db):
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state
    from infra.database import get_db

    upsert_connection(type="cli", provider="codex", status="available", config={"binary": "/bin/test"})
    legacy = seed_default_account('codex')
    second = add_account("codex", "Second")
    second = set_account_state(second["id"], "signed_in", expected_revision=0)
    third = add_account("codex", "Third")
    third = set_account_state(third["id"], "signed_in", expected_revision=0)
    for account in (second, third):
        (Path(account['source_home']) / 'auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'},
        }))
    db = get_db()
    for project in ("actor-project", "destination-project"):
        db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
                   (project, project, "/tmp/" + project))
    for thread, project in (("actor", "actor-project"), ("destination", "destination-project")):
        db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) VALUES (?,'user','codex/test',?,'t','t')",
                   (thread, project))
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('codex/test','Test','codex/test','codex','test','codex','t','t')")
    db.commit()
    yield db, legacy, second, third
    db.close()


@pytest.fixture
def vendors(accounts):
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state

    db, legacy, codex, third = accounts
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/synthetic"})
    claude = add_account("claude-code", "Claude Second")
    claude = set_account_state(claude["id"], "signed_in", expected_revision=0)
    return db, {"codex": codex, "claude-code": claude}


@pytest_asyncio.fixture
async def notification_owner(monkeypatch):
    from infra import app_loop, thread_execution, unavailable_model_recovery as recovery
    from infra.input_writes import begin_batch_write, settle_batch_write

    monkeypatch.setattr(app_loop, '_LOOP', asyncio.get_running_loop())
    monkeypatch.setattr(recovery, '_TASK', None)
    monkeypatch.setattr(recovery, '_RERUN', False)
    delivered = []

    async def synthetic_paid_edge(batch, **kwargs):
        # Everything through the actual durable classifier and claim has run.
        # Only provider transport is synthetic; use original delivery owners.
        begin_batch_write(batch, attempt_id='accepted-' + batch.turn_id, started_at='write')
        settle_batch_write('accepted-' + batch.turn_id, outcome='accepted')
        delivered.append(batch)
        return batch

    monkeypatch.setattr(thread_execution, 'execute_claimed_batch', synthetic_paid_edge)
    return delivered


@pytest.fixture
def held_worker(accounts):
    from connections.account_policy import select_account

    db, default, custom, other = accounts
    for identity, parent in [('parent', None), ('worker', 'parent')]:
        db.execute("INSERT INTO threads(id,user_id,project_id,spawned_by,model,created_at,updated_at) "
                   "VALUES (?,'user','destination-project',?,'codex/test','t','t')", (identity, parent))
    db.commit()
    step, plan, node = delegated(db)
    select_account('worker', 'codex', account_id=custom['id'], db=db)
    return db, default, custom, other, step, plan, node


@pytest.fixture
def no_stops(monkeypatch):
    stops = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", stops)
    return stops


@pytest.fixture
def isolated_shutdown_tasks(monkeypatch):
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from infra import state
    from jobs import corrections_realtime

    for attribute in ("_github_ownership_login_task", *(attr for _, attr in RUNTIME_LOOP_TASKS)):
        monkeypatch.setattr(state, attribute, None, raising=False)
    # The session app's classifier worker runs on its own loop; a test shutdown must not join it.
    monkeypatch.setattr(corrections_realtime, "WORKER", corrections_realtime.CorrectionsWorker())


@pytest.fixture
def claude(accounts):
    """A Claude thread bound to the Default account, with a second signed-in account."""
    from connections.account_policy import select_account
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state

    db = accounts[0]
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/synthetic"})
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES (?,'user',?,'destination-project','t','t')", (THREAD, MODEL))
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES (?,'Test',?,'claude-code','test','claude-code','t','t')", (MODEL, MODEL))
    db.commit()
    first = select_account(THREAD, "claude-code", db=db)
    other = add_account("claude-code", "Other")
    other = set_account_state(other["id"], "signed_in", expected_revision=0)
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('claude-turn',?,0,?,'running','t','test',?)", (THREAD, MODEL, first))
    db.commit()
    return SimpleNamespace(db=db, first=first, other=other["id"])
