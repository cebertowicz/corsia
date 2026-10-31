"""Usage recorded for a Turn stays with that Turn until it settles.

Pins: Turn usage receipts and their remainder, usage retained until settlement and along
harness paths, external-harness usage and its failures, recorded/recording/persisted usage
defects and their transactions, connection availability and account activity composition."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from plans import admission, commitment_cost, runner_events, service, usage_calls
from plans.usage_calls import binding_at_call_start
import pytest
from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

from connections import harness_usage
from external_harness_usage_helpers import (
    bind_plan_turn as _bind_plan_turn,
    bind_plan_turn,
    persist_settleable_turn,
    persist_turn as _persist_turn,
    persist_turn,
    stub_cc_terminal as _stub_cc_terminal,
    stub_cc_terminal,
    stub_codex_terminal as _stub_codex_terminal,
    stub_codex_terminal,
    usage_binding as _usage_binding,
    usage_row as _usage_row,
    usage_row,
    usage_rows as _usage_rows,
)
from infra import database, llm_usage, queries
from infra.database import get_db, write_transaction
from infra.llm_usage import (
    PlanUsagePersistenceError,
    reconcile_pending_turn_usage,
    record_llm_usage,
    UsageReceiptConflictError,
)
from infra.provider_usage import from_codex
from infra.schema import SCHEMA_SQL
from infra.thread_driver import finish_turn, TerminalResult
from infra.turn_usage_receipts import (
    _receipt_id,
    STARTUP_USAGE_RECEIPT_LIMIT,
    store_turn_usage_receipt,
)
from tests.test_child_parent_notice_stalled_child import _start
from vendor_account_fixtures import (  # noqa: F401
    _known_default,
    accounts,
    claude,
    CompetingRead,
    default_id,
    default_receipt,
    freeze_turn_on_default,
    held_worker,
    model_default_id,
    queue,
    ready,
    vendors,
)


def seed(held_worker, default=False):
    db, legacy, custom, _, step, plan, node = held_worker
    account = legacy if default else custom
    if default:
        db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='worker'", (account['id'],))
        _known_default(db, account)
    _start(db, node)
    db.commit()
    queue(db)
    db.execute("UPDATE messages SET delivery_error='account_model_not_discovered',write_outcome='proved_no_write',"
               "target_model='codex/test',active_turn_id=NULL WHERE id='brief'")
    db.commit()
    return db, account, step, plan, node


def parity(db, plan):
    from plans import activity_projection, operator_projection
    from plans.boundary import PlanActor
    actor = PlanActor(None, 'user', None, kind='user')
    full = operator_projection.read_project('destination-project', actor=actor, db=db)
    compact = activity_projection.read_project('destination-project', actor=actor, db=db)
    detailed = {item['plan']['id']: item for item in full['plans']}
    assert compact['attention'] == full['attention']
    for row in compact['plans']:
        original = detailed[row['plan']['id']]
        assert row['display'] == {key: original['display'][key] for key in row['display']}
        assert row['progress'] == {key: original['progress'][key] for key in row['progress']}
        assert row['has_nodes'] == bool(original['nodes'])
    own = operator_projection.read_thread('worker', actor=actor, db=db)['plans'][0]
    assert own['plan']['id'] == plan.id
    assert own['nodes'][0]['display']['label_key'] == 'waiting'
    assert own['progress']['liveness'] != 'advancing'
    return own, full


def _db():
    return get_db()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()


def _turn(db, turn_id: str, model: str) -> str:
    thread_id = f"thread-{turn_id}"
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?, 'user', ?, 't', 't')",
        (thread_id, model),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?,?,0,?,'running','t','test-boot')",
        (turn_id, thread_id, model),
    )
    db.commit()
    if model.startswith(("claude-code/", "codex/")):
        freeze_turn_on_default(db, turn_id)
    return thread_id


def _refusals(monkeypatch) -> tuple[str, str]:
    """The vendors' exact refusal wording, with a reset that is still in the future."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    target = datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=2)
    claude = f"You've hit your weekly limit · resets {target:%b} {target.day} at 10pm"
    codex = (
        f"You've hit your usage limit. Upgrade to Pro, or try again at "
        f"{target:%b} {target.day}st, {target.year} 2:32 AM."
    )
    return claude, codex


def _cc_live(monkeypatch, *, turn_id: str):
    """One Claude Code Turn with its durable side effects stubbed out."""
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_state import _Live

    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        model="opus",
        catalog_model_id="claude-code/opus",
    )
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity(turn_id, 1)
    _persist_turn("cc-session", turn_id)
    live.turn_active = True
    _stub_cc_terminal(monkeypatch)
    return live


def _defect_payloads(node_id: str) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [
            json.loads(row["payload_json"] or "{}")
            for row in db.execute(
                "SELECT payload_json FROM plan_node_events "
                "WHERE node_id=? AND event='usage_recording_failed' ORDER BY rowid",
                (node_id,),
            ).fetchall()
        ]
    finally:
        db.close()


def _recording_failures(commitment_id: str) -> int:
    from infra.database import get_db
    from plans import commitment_cost

    db = get_db()
    try:
        return commitment_cost.project(commitment_id, db=db)["usage_recording_failures"]
    finally:
        db.close()


def _refuse_writes(monkeypatch) -> None:
    monkeypatch.setattr(
        "plans.usage_calls.record_llm_usage",
        MagicMock(side_effect=PlanUsagePersistenceError("usage receipt could not be persisted")),
    )


async def _finish_cc_turn(monkeypatch, turn_id: str) -> None:
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity(turn_id, 1)
    live.turn_active = True
    stub_cc_terminal(monkeypatch)
    with patch("subagents.cc_event_result.insert_message", return_value="marker"):
        on_event = _make_on_event(live, "cc-session", MagicMock(), db=None)
        # No call reported a measurement: the terminal total is all there is.
        await on_event({"kind": "result", "stop_reason": "end_turn"})


async def _finish_codex_turn(monkeypatch, turn_id: str, *, measured: bool) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread", session=None, parent_thread_id=None,
        name="codex", model="gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity(turn_id, 1)
    stub_codex_terminal(monkeypatch)
    event: dict = {"kind": "result"}
    if measured:
        event["usage"] = {"input_tokens": 1, "cached_input_tokens": 1}
        event["token_volume"] = from_codex({"inputTokens": 1, "cachedInputTokens": 1})
    with patch("subagents.codex_runtime_events.insert_message", return_value="marker"):
        on_event = codex_runtime._make_on_event(live, "codex-session", MagicMock())
        await on_event(event)
        await live.delivery_event_tail


class _KeepOpen:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __getattr__(self, name: str):
        return getattr(self.db, name)

    def close(self) -> None:
        return None


class _FailUsageProjection(_KeepOpen):
    def execute(self, sql: str, params=()):
        if "INSERT INTO llm_usage" in sql:
            raise sqlite3.OperationalError("usage projection unavailable")
        return self.db.execute(sql, params)


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('thread-1','user-1','t0','t0')"
    )
    connection.execute(
        """INSERT INTO active_turns (
               id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
               plan_usage_role
           ) VALUES (
               'turn-1','thread-1',0,'test-model','completed','t0','boot-1',
               'plan-1','node-1','attempt-1','commitment-1','execution'
           )"""
    )
    connection.commit()
    yield connection
    connection.close()


def _record_through_projection(
    db: sqlite3.Connection,
    usage: dict | None,
    *,
    usage_key: str = "provider-call:1",
    fail_projection: bool = False,
) -> None:
    wrapper = _FailUsageProjection(db) if fail_projection else _KeepOpen(db)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("infra.database.get_db", lambda: wrapper)
        record_llm_usage(
            "thread-1",
            "test-model",
            usage,
            source="conversation:native",
            ttft_ms=17,
            turn_id="turn-1",
            usage_key=usage_key,
            _emit_product_event=lambda *_args, **_kwargs: None,
        )


SOURCE = "external_harness:claude-code"


def _row(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion, "cost_usd": None,
    }


def _receipt(key: str, prompt: int, completion: int, *, turn_id: str = "cc-turn") -> None:
    usage_calls.record_observed_call_usage(
        "cc-thread", "opus", _row(prompt, completion), source=SOURCE,
        turn_id=turn_id, usage_key=key,
    )


def _terminal(prompt: int, completion: int, *, turn_id: str | None = "cc-turn") -> bool:
    return usage_calls.record_turn_fallback(
        "cc-thread", "opus", _row(prompt, completion), source=SOURCE, turn_id=turn_id,
    )


def _totals() -> list[int]:
    db = get_db()
    try:
        return [
            r["total_tokens"] for r in db.execute(
                "SELECT total_tokens FROM llm_usage WHERE thread_id='cc-thread' "
                "ORDER BY created_at, total_tokens"
            ).fetchall()
        ]
    finally:
        db.close()


def _defect_event_names(node_id: str) -> list[str]:
    db = get_db()
    try:
        return [
            row["event"] for row in db.execute(
                "SELECT event FROM plan_node_events WHERE node_id=? AND event='usage_recording_failed'",
                (node_id,),
            ).fetchall()
        ]
    finally:
        db.close()


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        thread = db.execute(
            "SELECT title,next_message_seq FROM threads WHERE id='worker'",
        ).fetchone()
        events = db.execute(
            "SELECT node_id,event,payload_json FROM plan_node_events "
            "WHERE event='usage_recording_failed' ORDER BY rowid",
        ).fetchall()
        receipts = db.execute(
            "SELECT content,metadata,seq FROM messages WHERE thread_id='worker' ORDER BY seq",
        ).fetchall()
        return thread, events, receipts


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.02)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _report(node_id):
    return usage_calls.record_call_usage(
        "worker", "test-model", {}, source="conversation",
        turn_id="turn-1", usage_key="provider-call:1",
        binding={"plan_node_id": node_id},
    )


@pytest.fixture
def defect_case(isolated_db, monkeypatch):
    persist_turn("worker", "turn-1")
    with database.write_transaction(None, scope="seed_usage_defect_test") as db:
        db.execute("UPDATE threads SET title='original' WHERE id='worker'")
        plan = service.ensure_plan("thread", "worker", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Measured task", acceptance_criteria="Done", db=db,
        )
    # This unit begins after usage recording. The accepted full-path suite
    # separately covers that ledger; real notification writes remain intact.
    monkeypatch.setattr(usage_calls, "record_llm_usage", lambda *args, **kwargs: None)
    return isolated_db, node.id


MODEL = "anthropic/claude-sonnet-4-6"


SMALL_MEASURED = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


@pytest.fixture
def usage_scope(isolated_db):
    with database.write_transaction(None, scope="usage_transaction_seed") as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('usage-project','Original','t','t')",
        )
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('usage-thread','fixture-user','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,"
            "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
            "VALUES ('usage-turn','usage-thread',0,?,'completed','t','boot',"
            "'usage-plan','usage-node','usage-attempt','usage-commitment','execution')",
            (MODEL,),
        )
    return isolated_db


def _snapshot(path):
    with closing(sqlite3.connect(str(path))) as db:
        return (
            db.execute("SELECT name FROM projects WHERE id='usage-project'").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0],
            db.execute(
                "SELECT usage_receipts_json FROM active_turns WHERE id='usage-turn'",
            ).fetchone()[0],
        )


def _writer_free(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
    return True


MEASURED = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def _selected_task(db):
    plan = service.ensure_plan("thread", "worker", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Task A", acceptance_criteria="Done", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    admission.select(node.id, actor_id="worker", provenance="test", db=db, touch=False)
    db.commit()
    return node


def _defect_payloads_in_db(db, node_id: str) -> list[dict]:
    return [
        json.loads(row["payload_json"] or "{}")
        for row in db.execute(
            "SELECT payload_json FROM plan_node_events "
            "WHERE node_id=? AND event='usage_recording_failed' ORDER BY rowid",
            (node_id,),
        ).fetchall()
    ]


def _receipts(db) -> list[str]:
    return [
        row["content"] for row in db.execute(
            "SELECT content,metadata FROM messages WHERE thread_id='worker'"
        ).fetchall()
        if json.loads(row["metadata"] or "{}").get("event_type") == "usage.recording_failed"
    ]


@contextmanager
def _receipt_store_locked():
    real = database.write_transaction

    @contextmanager
    def refuse(db, *, scope):
        if scope == "store_turn_usage_receipt":
            raise sqlite3.OperationalError("database is locked")
        with real(db, scope=scope) as connection:
            yield connection

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("infra.turn_usage_receipts.write_transaction", refuse)
        yield


def _token_pairs() -> list[tuple]:
    db = get_db()
    try:
        return [
            tuple(row) for row in db.execute(
                "SELECT prompt_tokens,completion_tokens FROM llm_usage "
                "ORDER BY prompt_tokens"
            ).fetchall()
        ]
    finally:
        db.close()


def _refuse_defect_report():
    from infra.llm_usage import PlanUsagePersistenceError

    async def cannot_record(*_args, **_kwargs):
        raise PlanUsagePersistenceError("usage defect could not be reported")

    return cannot_record


class _WriterLock:
    """The write lock held by another writer for the named transaction scopes."""

    def __init__(self, *scopes: str) -> None:
        self.scopes = set(scopes)

    @contextmanager
    def transaction(self, db, *, scope: str):
        if scope in self.scopes:
            raise sqlite3.OperationalError("database is locked")
        with database.write_transaction(db, scope=scope) as connection:
            yield connection


@contextmanager
def _lock_window(*scopes: str):
    lock = _WriterLock(*(scopes or ("store_turn_usage_receipt", "direct_llm_usage")))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("infra.turn_usage_receipts.write_transaction", lock.transaction)
        patch.setattr("infra.llm_usage.write_transaction", lock.transaction)
        yield


def _finish(turn_id: str = "turn-1"):
    return finish_turn(
        turn_id, outcome="completed", ended_at="t9",
        successor_turn_id=f"{turn_id}-next", boot_epoch="test-boot",
    )


def _query(sql: str, params=()) -> list[sqlite3.Row]:
    db = get_db()
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _retained_turn_usage_rows() -> list[sqlite3.Row]:
    return _query(
        "SELECT id,turn_id,total_tokens,ttft_ms,source,plan_id,plan_node_id "
        "FROM llm_usage ORDER BY total_tokens,id"
    )


def _defect_reasons() -> list[str]:
    return [
        json.loads(row["payload_json"])["reason"]
        for row in _query(
            "SELECT payload_json FROM plan_node_events "
            "WHERE event='usage_recording_failed' ORDER BY rowid"
        )
    ]


def _record_worker_usage(turn_id: str | None = "turn-1", key: str | None = "provider-call:1", **kw):
    from infra.llm_usage import record_llm_usage

    record_llm_usage(
        "worker", "test-model", kw.pop("usage", MEASURED), source="conversation",
        turn_id=turn_id, usage_key=key, _emit_product_event=lambda *a, **k: None, **kw,
    )


def _plan_bound_turn() -> tuple:
    """Turn snapshot on task A; the executor has since moved to task B."""
    from plans import admission, service

    persist_settleable_turn("worker", "turn-1")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        first, second = (
            service.add_node(plan.id, kind="task", title=t, acceptance_criteria="Done", db=db)
            for t in ("Task A", "Task B")
        )
        for node in (first, second):
            service.transition_node(node.id, "active", db=db)
        admission.select(first.id, actor_id="worker", provenance="test", db=db, touch=False)
        db.execute(
            "UPDATE active_turns SET plan_id=?,plan_node_id=?,plan_attempt_id='attempt-a',"
            "plan_commitment_id=?,plan_usage_role='execution' WHERE id='turn-1'",
            (plan.id, first.id, first.commitment_id),
        )
        admission.release(first.id, "accepted", db=db)
        admission.select(second.id, actor_id="worker", provenance="test", db=db, touch=False)
        db.commit()
        return first, second
    finally:
        db.close()


@pytest.mark.parametrize('default', [False, True], ids=['added', 'default'])
@pytest.mark.parametrize('state', ['signed_out', 'limited', 'unread', 'ready', 'missing', 'stale'])
def test_full_compact_account_facts_and_copy_are_identical(held_worker, default, state):
    from connections import read_account_readiness
    from connections.harness_usage import record_limited, stored_usage
    from connections.account_receipts import capture_receipt
    from plans import activity_facts, operator_stopped, service, wait_causes
    from prompts.plan_wakes import account_readiness_detail, ACCOUNT_READINESS_UNREAD, ACCOUNT_PENDING_DETAIL
    db, account, step, plan, node = seed(held_worker, default)
    if state in ('ready', 'limited', 'stale'):
        ready(db, account)
    if state == 'signed_out':
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (account['id'],))
    elif state == 'unread':
        db.execute("UPDATE threads SET model='unresolved' WHERE id='worker'")
        db.execute("UPDATE messages SET target_model='unresolved' WHERE id='brief'")
    elif state == 'missing':
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DELETE FROM vendor_accounts WHERE id=?', (account['id'],))
    elif state == 'stale':
        # The fence reads the account's own login-generation stamp, so age that alongside the
        # row counter: a bare revision bump changes no evidence at all.
        db.execute('UPDATE vendor_accounts SET revision=revision+1,'
                   "discovery_json=json_set(discovery_json,'$.login_revision',revision+1) WHERE id=?",
                   (account['id'],))
    elif state == 'limited':
        record_limited(provider='codex',
                       receipt=capture_receipt('codex', account_id=account['id'], db=db),
                       reason='plan_limit', resets_at='2099-01-01T00:00:00+00:00', source='synthetic', db=db)
        assert stored_usage(provider='codex', account_id=account['id'], db=db)['vendor_verdicts']['null']['state'] == 'limited'
    db.commit()
    before = tuple(db.iterdump())
    readiness = read_account_readiness(['worker'], db=db).get('worker')
    detail = account_readiness_detail(readiness)
    if state in ('unread', 'missing'):
        assert detail == ACCOUNT_READINESS_UNREAD
    elif state in ('ready', 'limited'):
        assert detail == ACCOUNT_PENDING_DETAIL
    elif state == 'signed_out':
        assert 'it is signed out' in detail
    else:
        assert 'selected model has not been discovered' in detail
    rows = activity_facts.compact_nodes([plan.id], db=db)
    plan_row = dict(service._plan_row(db, plan.id))
    compact_facts = activity_facts.executor_stop_facts(rows, {plan.id: plan_row}, db=db)
    shared = operator_stopped.executor_facts({'worker'}, db=db)
    assert compact_facts[node.id] == shared['worker'] == {'stalled': 'account', 'account_readiness': readiness}
    own, full = parity(db, plan)
    assert own['nodes'][0]['status_reason'] == 'Stalled: its input is held for its account ' + detail
    parent_plan = service.ensure_plan('thread', 'parent', db=db)
    pairs = [(service._node_row(db, step.id), service._plan_row(db, parent_plan.id))]
    child_fact = wait_causes.delegation_facts(pairs, db=db)[step.id]
    assert child_fact['account_readiness'] == readiness
    assert 'The delegated child is stalled: its input is held for its account ' + detail in str(full)
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('mismatch', [None, 'plan_id', 'plan_node_id', 'plan_attempt_id'])
def test_account_hold_cannot_be_hidden_by_matched_or_unrelated_reset(held_worker, mismatch):
    from plans import operator_stopped
    db, account, _, plan, node = seed(held_worker)
    attempt = db.execute('SELECT id FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL', (node.id,)).fetchone()[0]
    identity = {'plan_id': plan.id, 'plan_node_id': node.id, 'plan_attempt_id': attempt}
    if mismatch:
        identity[mismatch] = 'unrelated'
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,"
               "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES ('reset-turn','worker',0,'codex/test','error','t','b',?,?,?,?,'execution')",
               (*identity.values(), db.execute('SELECT commitment_id FROM plan_nodes WHERE id=?', (node.id,)).fetchone()[0]))
    db.execute("INSERT INTO plan_liveness_wakes(id,thread_id,status,deadline,provider_reset_at,provider_reset_turn_id,"
               "cycle,last_open_count,last_signal_hash,last_settled_events,wakes_in_window,armed_at,updated_at) "
               "VALUES ('reset','worker','armed','2099-01-01T12:00:00+00:00','2099-01-01T12:00:00+00:00',"
               "'reset-turn',0,0,'',0,0,'t','t')")
    db.commit()
    fact = operator_stopped.executor_facts({'worker'}, db=db)['worker']
    assert fact['provider_reset_at'] == '2099-01-01T12:00:00+00:00'
    assert all(fact[key] == value for key, value in identity.items())
    own, _ = parity(db, plan)
    assert 'its input is held for its account' in own['nodes'][0]['status_reason']
    assert 'Verify the signed-in account' in own['nodes'][0]['status_reason']
    assert 'Provider resets' not in own['nodes'][0]['status_reason']


@pytest.mark.parametrize('consumer', ['executor', 'delegation'])
def test_compact_fact_boundary_keeps_kind_and_readiness_in_same_snapshot(held_worker, consumer):
    from plans import activity_facts, service, wait_causes
    from connections import read_account_readiness
    db, account, step, plan, node = seed(held_worker)
    ready(db, account)
    path = db.execute('PRAGMA database_list').fetchone()[2]
    competitor = sqlite3.connect(path)
    observed = CompetingRead(db, competitor, account['id'])
    assert not db.in_transaction
    try:
        if consumer == 'executor':
            rows = activity_facts.compact_nodes([plan.id], db=db)
            facts = activity_facts.executor_stop_facts(rows, {plan.id: dict(service._plan_row(db, plan.id))}, db=observed)
            fact = facts[node.id]
        else:
            parent = service.ensure_plan('thread', 'parent', db=db)
            db.commit()
            fact = wait_causes.delegation_facts([(service._node_row(db, step.id), service._plan_row(db, parent.id))], db=observed)[step.id]
        assert fact['stalled'] == 'account'
        assert fact['account_readiness'] == {'ready': True, 'cause': None}
        assert observed.fired and not db.in_transaction
        assert read_account_readiness(['worker'], db=db)['worker']['cause'] == 'account_signed_out'
    finally:
        competitor.close()


@pytest.mark.parametrize('outer', [False, True])
def test_snapshot_works_with_execute_only_proxy_and_preserves_outer_transaction(held_worker, outer):
    from plans.wait_causes import account_read_snapshot
    db = held_worker[0]
    db.commit()
    if outer:
        db.execute("UPDATE threads SET title='uncommitted' WHERE id='worker'")
    class ExecuteOnly:
        def execute(self, sql, parameters=()):
            return db.execute(sql, parameters)
    with account_read_snapshot(ExecuteOnly()):
        assert db.execute("SELECT id FROM threads WHERE id='worker'").fetchone()[0] == 'worker'
    assert db.in_transaction == outer
    if outer:
        db.rollback()
        assert db.execute("SELECT title FROM threads WHERE id='worker'").fetchone()[0] != 'uncommitted'


@pytest.fixture
def usage_default_accounts(isolated_db):
    from connections.vendor_accounts import set_account_state
    from vendor_account_fixtures import seed_default_account

    for provider in ('codex', 'claude-code'):
        account = seed_default_account(provider)
        set_account_state(account['id'], 'signed_in', expected_revision=account['revision'])


@pytest.mark.usefixtures('usage_default_accounts')
def test_availability_projects_allowed_limited_and_unknown_states(isolated_db) -> None:
    db = _db()
    harness_usage.record_allowed(provider="claude-code", source="test", db=db, receipt=default_receipt("claude-code", db=db))
    assert harness_usage.connection_availability("claude-code/opus", db=db, account_id=model_default_id("claude-code/opus", db=db)).state == "allowed"

    reset = _future()
    harness_usage.record_limited(
        provider="codex", reason="limit", resets_at=reset, source="test", db=db, receipt=default_receipt("codex", db=db),
    )
    limited = harness_usage.connection_availability("codex/gpt-5-codex", db=db, account_id=model_default_id("codex/gpt-5-codex", db=db))
    assert (limited.state, limited.connection, limited.resets_at) == (
        "limited_with_reset", "codex", reset,
    )

    harness_usage.record_limited(
        provider="claude-code", reason="session", resets_at=None, source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )
    assert harness_usage.connection_availability("claude-code/opus", db=db, account_id=model_default_id("claude-code/opus", db=db)).state == "limited_without_reset"
    assert harness_usage.connection_availability("omlx@local/qwen", db=db, account_id=model_default_id("omlx@local/qwen", db=db)).state == "unknown"


@pytest.mark.usefixtures('usage_default_accounts')
def test_availability_uses_model_scoped_verdict_without_blocking_sibling(isolated_db) -> None:
    db = _db()
    harness_usage.record_limited(
        provider="claude-code", reason="seven_day_sonnet", resets_at=_future(),
        source="test", scope_model="sonnet", db=db, receipt=default_receipt("claude-code", db=db),
    )

    assert harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state == "limited_with_reset"
    assert harness_usage.connection_availability("claude-code/opus", db=db, account_id=model_default_id("claude-code/opus", db=db)).state == "unknown"

    from connections.claude_usage import apply_rate_limit_event

    apply_rate_limit_event({"status": "allowed"}, db=db, receipt=default_receipt('claude-code', db=db))
    assert harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state == "limited_with_reset"


@pytest.mark.usefixtures('usage_default_accounts')
def test_past_reset_is_not_limited_and_a_transactional_read_sees_its_verdict(isolated_db) -> None:
    db = _db()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    harness_usage.record_limited(provider="codex", reason="expired", resets_at=past, source="test", db=db, receipt=default_receipt("codex", db=db))
    assert harness_usage.connection_availability("codex/gpt-5-codex", db=db, account_id=model_default_id("codex/gpt-5-codex", db=db)).state == "unknown"

    with write_transaction(db, scope="availability_test") as connection:
        harness_usage.record_limited(
            provider="claude-code", reason="uncommitted", resets_at=_future(), source="test", db=connection, receipt=default_receipt("claude-code", db=connection),
        )
        assert harness_usage.connection_availability("claude-code/opus", db=connection, account_id=model_default_id("claude-code/opus", db=connection)).state == "limited_with_reset"


@pytest.mark.usefixtures('usage_default_accounts')
def test_terminal_refusal_records_limit_before_successor_claim(isolated_db, monkeypatch) -> None:
    db = _db()
    claude, _codex = _refusals(monkeypatch)
    _turn(db, "turn-claude", "claude-code/sonnet")
    seen: list[str] = []

    def observe_successor(thread_id, *, db, **_kwargs):
        seen.append(harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state)
        return None

    monkeypatch.setattr("infra.thread_driver.start_idle_turn", observe_successor)
    finish_turn(
        "turn-claude", outcome="error", ended_at="t", successor_turn_id="next", boot_epoch="test-boot",
        error=claude, db=db,
    )

    availability = harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db))
    assert seen == ["limited_with_reset"]
    assert availability.state == "limited_with_reset"
    assert availability.resets_at is not None
    assert availability.reason is not None and len(availability.reason) <= 300


@pytest.mark.usefixtures('usage_default_accounts')
def test_codex_terminal_refusal_parses_its_reset_and_non_limit_error_records_nothing(
    isolated_db, monkeypatch,
) -> None:
    db = _db()
    _claude, codex = _refusals(monkeypatch)
    _turn(db, "turn-codex", "codex/gpt-5-codex")
    finish_turn(
        "turn-codex", outcome="error", ended_at="t", successor_turn_id="next", boot_epoch="test-boot",
        error=codex, db=db,
    )
    availability = harness_usage.connection_availability("codex/gpt-5-codex", db=db, account_id=model_default_id("codex/gpt-5-codex", db=db))
    assert availability.state == "limited_with_reset"
    assert availability.resets_at is not None

    _turn(db, "turn-other", "claude-code/opus")
    finish_turn(
        "turn-other", outcome="error", ended_at="t", successor_turn_id="next", boot_epoch="test-boot",
        error="socket closed", db=db,
    )
    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)) is None


@pytest.mark.usefixtures('usage_default_accounts')
def test_refusal_is_attributed_to_the_turn_model_not_the_thread_model(isolated_db, monkeypatch) -> None:
    db = _db()
    claude, _codex = _refusals(monkeypatch)
    thread_id = _turn(db, "turn-switched", "claude-code/sonnet")
    db.execute("UPDATE threads SET model='codex/gpt-5-codex' WHERE id=?", (thread_id,))
    db.commit()
    finish_turn(
        "turn-switched", outcome="error", ended_at="t", successor_turn_id="next",
        boot_epoch="test-boot", error=claude, db=db,
    )
    assert harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state == "limited_with_reset"
    assert harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)) is None


@pytest.mark.usefixtures('usage_default_accounts')
def test_completed_post_reset_turn_clears_its_scoped_verdict(isolated_db) -> None:
    db = _db()
    reset = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    harness_usage.record_limited(
        provider="claude-code", reason="weekly", resets_at=reset,
        scope_model="sonnet", source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )
    started = datetime.now(timezone.utc) + timedelta(seconds=1)
    _turn(db, "post-reset", "claude-code/sonnet")
    db.execute("UPDATE active_turns SET started_at=? WHERE id='post-reset'", (started.isoformat(),))
    db.commit()

    finish_turn(
        "post-reset", outcome="completed", ended_at=(started + timedelta(seconds=1)).isoformat(),
        successor_turn_id="next", boot_epoch="test-boot", db=db,
    )

    assert harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state == "allowed"


@pytest.mark.usefixtures('usage_default_accounts')
def test_refusal_observed_after_turn_start_is_never_overwritten(isolated_db) -> None:
    db = _db()
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    reset = (started - timedelta(minutes=1)).isoformat()
    _turn(db, "late-refusal", "claude-code/sonnet")
    db.execute("UPDATE active_turns SET started_at=? WHERE id='late-refusal'", (started.isoformat(),))
    db.commit()
    harness_usage.record_limited(
        provider="claude-code", reason="weekly", resets_at=reset,
        scope_model="sonnet", source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )

    finish_turn(
        "late-refusal", outcome="completed", ended_at=datetime.now(timezone.utc).isoformat(),
        successor_turn_id="next", boot_epoch="test-boot", db=db,
    )

    verdict = harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["vendor_verdicts"]["sonnet"]
    assert verdict["state"] == "limited"


@pytest.mark.usefixtures('usage_default_accounts')
def test_completed_pre_reset_turn_never_clears_expired_verdict(isolated_db) -> None:
    db = _db()
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    reset = (started + timedelta(minutes=1)).isoformat()
    _turn(db, "pre-reset", "claude-code/sonnet")
    db.execute("UPDATE active_turns SET started_at=? WHERE id='pre-reset'", (started.isoformat(),))
    db.commit()
    harness_usage.record_limited(
        provider="claude-code", reason="weekly", resets_at=reset,
        scope_model="sonnet", source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )

    finish_turn(
        "pre-reset", outcome="completed", ended_at=datetime.now(timezone.utc).isoformat(),
        successor_turn_id="next", boot_epoch="test-boot", db=db,
    )

    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["vendor_verdicts"]["sonnet"]["state"] == "limited"


@pytest.mark.parametrize("error", [
    "Server busy, try again at a later time",
    "Rate limit exceeded for requests per minute; try again at 12:01",
])
def test_generic_retry_wording_is_not_an_account_limit(error: str) -> None:
    assert is_provider_usage_or_quota_limit(error) is False


@pytest.mark.asyncio
async def test_claude_code_result_records_provider_usage(isolated_db, monkeypatch):
    """A Claude Code terminal result writes its final-call token measurement."""
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        model="opus",
        catalog_model_id="claude-code/opus",
    )
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-turn", 1)
    _persist_turn("cc-session", "cc-turn")
    binding = _bind_plan_turn("cc-session", "cc-turn")
    live.turn_active = True
    _stub_cc_terminal(monkeypatch)

    with patch(
        "subagents.cc_event_result.insert_message", return_value="marker",
    ):
        on_event = _make_on_event(live, "cc-session", MagicMock(), db=None)
        await on_event({
            "kind": "final_call_usage",
            "message_id": "msg_01",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 45,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 90,
            },
        })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    assert tuple(_usage_row()) == (
        "claude-code/opus", 120, 45, 30, 90, 285,
        "external_harness:claude-code",
    )
    assert _usage_binding() == binding


@pytest.mark.asyncio
async def test_claude_code_bills_every_call_in_a_multi_call_turn(
    isolated_db, monkeypatch,
):
    """A Turn is many calls; the ledger records their sum, not the last one.

    Keeping only the final call is the undercount this contract removes: a
    tool-using Turn can make a dozen calls and bill one.
    """
    from subagents.cc_runtime_events import _make_on_event

    live = _cc_live(monkeypatch, turn_id="cc-multi-call")

    with patch(
        "subagents.cc_event_result.insert_message", return_value="marker",
    ):
        on_event = _make_on_event(live, "cc-session", MagicMock(), db=None)
        for index, call in enumerate((
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": 0,
            },
            {
                "input_tokens": 5,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1_100,
            },
            {
                "input_tokens": 7,
                "output_tokens": 30,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1_100,
            },
        ), start=1):
            await on_event({
                "kind": "final_call_usage", "message_id": f"msg_{index}", "usage": call,
            })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    # Each call receipts its own row; billing only the final call would have
    # recorded 1_137 of 3_372, or only the first, 1_110.
    rows = [tuple(row) for row in _usage_rows()]
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"claude-code/opus"}
    assert {row[6] for row in rows} == {"external_harness:claude-code"}
    # Sum of three calls: 112 uncached, 60 output, 1_000 written, 2_200 read.
    summed = tuple(sum(row[i] for row in rows) for i in range(1, 6))
    assert summed == (112, 60, 1_000, 2_200, 3_372)


@pytest.mark.asyncio
async def test_claude_post_retarget_usage_collapse_forces_cold_reconstruction(
    isolated_db,
    monkeypatch,
    caplog,
):
    """A nearly empty normalized first call distrusts the retargeted handle."""
    import logging

    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        model="sonnet",
        catalog_model_id="claude-code/sonnet",
        post_retarget_context_usage_floor=20_000,
    )
    live.provider_reported_model = "sonnet"
    live.corsia_turn = TurnIdentity("cc-retarget-collapse", 1)
    _persist_turn("cc-session", "cc-retarget-collapse")
    live.turn_active = True
    caplog.set_level(logging.ERROR, logger="subagents.cc_runtime_events")
    _stub_cc_terminal(monkeypatch)

    with patch(
        "subagents.cc_event_result.insert_message", return_value="marker",
    ):
        on_event = _make_on_event(live, "cc-session", MagicMock(), db=None)
        await on_event({
            "kind": "final_call_usage",
            "message_id": "msg_01",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 45,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 400,
            },
        })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    # Effective input is 1,000 (= prompt + cache-write + cache-read), only 5%
    # of the 20k measured floor and below the conservative 10% detector.
    assert live.provider_context_untrusted is True
    assert live.post_retarget_context_usage_floor is None
    assert "post-retarget continuity collapse" in caplog.text
    assert tuple(_usage_row()) == (
        "claude-code/sonnet", 500, 45, 100, 400, 1_045,
        "external_harness:claude-code",
    )


@pytest.mark.asyncio
async def test_codex_result_records_provider_usage(isolated_db, monkeypatch):
    """A Codex terminal writes the turn's volume, split into disjoint columns.

    The terminal event carries ``token_volume`` -- the delta of Codex's
    thread-cumulative counter across this turn -- because ``usage`` is only the
    final occupancy reading.
    """
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity("codex-turn", 1)
    _persist_turn("codex-session", "codex-turn")
    binding = _bind_plan_turn("codex-session", "codex-turn")
    _stub_codex_terminal(monkeypatch)

    with patch(
        "subagents.codex_runtime_events.insert_message", return_value="marker",
    ):
        on_event = codex_runtime._make_on_event(
            live, "codex-session", MagicMock(),
        )
        await on_event({
            "kind": "result",
            "usage": {
                "input_tokens": 2_000,
                "cached_input_tokens": 1_600,
                "model_context_window": 258_400,
            },
            "token_volume": from_codex({
                "inputTokens": 2_000,
                "cachedInputTokens": 1_600,
                "outputTokens": 300,
            }),
        })
        await live.delivery_event_tail

    # 2_000 input of which 1_600 was cached: 400 uncached + 1_600 cache read,
    # plus 300 output = 2_300 total.
    assert tuple(_usage_row()) == (
        "codex/gpt-5-codex", 400, 300, 0, 1_600, 2_300,
        "external_harness:codex",
    )
    assert _usage_binding() == binding


@pytest.mark.asyncio
async def test_cc_usage_write_failure_is_loud_without_blocking_terminal(
    isolated_db, monkeypatch, caplog,
):
    """A usage-write failure cannot wedge an otherwise completed CC turn."""
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-write-failure", 1)
    live.turn_active = True
    bcast = MagicMock()
    caplog.set_level(logging.ERROR, logger="subagents.cc_runtime_events")
    stub_cc_terminal(monkeypatch)
    monkeypatch.setattr(
        "plans.usage_calls.record_llm_usage",
        MagicMock(side_effect=RuntimeError("usage database unavailable")),
    )

    with patch("subagents.cc_event_result.insert_message", return_value="marker"):
        on_event = _make_on_event(live, "cc-session", bcast, db=None)
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    assert usage_row() is None
    assert "plan-bound usage handoff incomplete" in caplog.text


@pytest.mark.asyncio
async def test_cc_malformed_usage_is_loud_and_writes_an_unknown_row(
    isolated_db, monkeypatch, caplog,
):
    """Malformed CC usage remains visible and records unknown cost."""
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-malformed-usage", 1)
    persist_turn("cc-session", "cc-malformed-usage")
    live.turn_active = True
    bcast = MagicMock()
    caplog.set_level(logging.WARNING, logger="subagents.cc_runtime_events")
    stub_cc_terminal(monkeypatch)

    with patch("subagents.cc_event_result.insert_message", return_value="marker"):
        on_event = _make_on_event(live, "cc-session", bcast, db=None)
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": "not-a-token-count"},
        })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    row = usage_row()
    assert tuple(row) == (
        "claude-code/opus", None, None, None, None, None,
        "external_harness:claude-code",
    )
    assert "recording unknown terminal usage" in caplog.text


@pytest.mark.asyncio
async def test_codex_usage_write_failure_is_loud_without_blocking_terminal(
    isolated_db, monkeypatch, caplog,
):
    """A usage-write failure cannot wedge an otherwise completed Codex turn."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity("codex-write-failure", 1)
    bcast = MagicMock()
    caplog.set_level(logging.ERROR, logger="subagents.codex_runtime_terminal")
    stub_codex_terminal(monkeypatch)
    monkeypatch.setattr(
        "plans.usage_calls.record_llm_usage",
        MagicMock(side_effect=RuntimeError("usage database unavailable")),
    )

    with patch("subagents.codex_runtime_events.insert_message", return_value="marker"):
        on_event = codex_runtime._make_on_event(live, "codex-session", bcast)
        await on_event({
            "kind": "result",
            "usage": {"input_tokens": 1, "cached_input_tokens": 1},
            "token_volume": from_codex({"inputTokens": 1, "cachedInputTokens": 1}),
        })
        await live.delivery_event_tail

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    assert usage_row() is None
    assert "plan-bound usage handoff incomplete" in caplog.text


@pytest.mark.asyncio
async def test_codex_terminal_marker_carries_a_usage_failure_the_entry_could_not_record(
    isolated_db, monkeypatch,
):
    """When even the defect could not be written, the Turn's result record says so."""
    from core.turn_core import TurnIdentity
    from infra.llm_usage import PlanUsagePersistenceError
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread", session=None, parent_thread_id=None,
        name="codex", model="gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity("codex-defect-failure", 1)
    bcast = MagicMock()
    stub_codex_terminal(monkeypatch)
    monkeypatch.setattr(
        "subagents.codex_runtime_terminal.record_turn_fallback",
        MagicMock(side_effect=PlanUsagePersistenceError("usage defect could not be reported")),
    )

    with patch("subagents.codex_runtime_events.insert_message", return_value="marker") as insert:
        on_event = codex_runtime._make_on_event(live, "codex-session", bcast)
        await on_event({
            "kind": "result",
            "usage": {"input_tokens": 1, "cached_input_tokens": 1},
            "token_volume": from_codex({"inputTokens": 1, "cachedInputTokens": 1}),
        })
        await live.delivery_event_tail

    assert any(call.args[1].get("type") == "turn_complete" for call in bcast.call_args_list)
    metadata = insert.call_args.args[3]
    assert "usage defect could not be reported" in metadata["usage_accounting_failed"]


@pytest.mark.asyncio
async def test_codex_malformed_usage_is_loud_and_writes_an_unknown_row(
    isolated_db, monkeypatch, caplog,
):
    """Malformed Codex usage remains visible and records unknown cost."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity("codex-malformed-usage", 1)
    persist_turn("codex-session", "codex-malformed-usage")
    bcast = MagicMock()
    caplog.set_level(logging.WARNING, logger="subagents.codex_runtime_events")
    stub_codex_terminal(monkeypatch)

    with patch("subagents.codex_runtime_events.insert_message", return_value="marker"):
        on_event = codex_runtime._make_on_event(live, "codex-session", bcast)
        await on_event({
            "kind": "result",
            "usage": {"input_tokens": "not-a-token-count"},
            "token_volume": from_codex({"inputTokens": "not-a-token-count"}),
        })
        await live.delivery_event_tail

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    row = usage_row()
    assert tuple(row) == (
        "codex/gpt-5-codex", None, None, None, None, None,
        "external_harness:codex",
    )
    assert "recording unknown terminal usage" in caplog.text


@pytest.mark.asyncio
async def test_cc_plan_bound_usage_failure_still_completes_the_turn(
    isolated_db, monkeypatch, caplog,
):
    """The escalating branch must not become a wedged turn.

    The generic-failure tests above take the `except Exception` path. This one
    raises the plan-bound error itself, which used to be re-raised into a
    caller whose terminal work runs under try/finally with no except — so the
    turn_complete broadcast and plan completion never ran and the user lost a
    result that had already been produced.
    """
    from core.turn_core import TurnIdentity
    from infra.llm_usage import PlanUsagePersistenceError
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-plan-bound-failure", 1)
    live.turn_active = True
    bcast = MagicMock()
    caplog.set_level(logging.ERROR, logger="subagents.cc_runtime_events")
    stub_cc_terminal(monkeypatch)
    monkeypatch.setattr(
        "plans.usage_calls.record_llm_usage",
        MagicMock(side_effect=PlanUsagePersistenceError("plan-bound usage")),
    )

    with patch("subagents.cc_event_result.insert_message", return_value="marker"):
        on_event = _make_on_event(live, "cc-session", bcast, db=None)
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
        await on_event({"kind": "result", "stop_reason": "end_turn"})

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    assert usage_row() is None
    assert "plan-bound usage handoff incomplete" in caplog.text


@pytest.mark.asyncio
async def test_codex_plan_bound_usage_failure_still_completes_the_turn(
    isolated_db, monkeypatch, caplog,
):
    """Codex shares the boundary: a lost ledger row never eats the result."""
    from core.turn_core import TurnIdentity
    from infra.llm_usage import PlanUsagePersistenceError
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
    )
    live.provider_reported_model = "gpt-5-codex"
    live.corsia_turn = TurnIdentity("codex-plan-bound-failure", 1)
    bcast = MagicMock()
    caplog.set_level(logging.ERROR, logger="subagents.codex_runtime_terminal")
    stub_codex_terminal(monkeypatch)
    monkeypatch.setattr(
        "plans.usage_calls.record_llm_usage",
        MagicMock(side_effect=PlanUsagePersistenceError("plan-bound usage")),
    )

    with patch("subagents.codex_runtime_events.insert_message", return_value="marker"):
        on_event = codex_runtime._make_on_event(live, "codex-session", bcast)
        await on_event({
            "kind": "result",
            "usage": {"input_tokens": 1, "cached_input_tokens": 1},
            "token_volume": from_codex({"inputTokens": 1, "cachedInputTokens": 1}),
        })
        await live.delivery_event_tail

    assert any(call.args[1].get("type") == "turn_complete"
               for call in bcast.call_args_list)
    assert usage_row() is None
    assert "plan-bound usage handoff incomplete" in caplog.text


def test_a_vanished_turn_row_records_unbound_usage_instead_of_escalating(isolated_db):
    """A missing Turn row is unknown attribution, not a plan-bound failure.

    Ordinary terminal cleanup and restart drains both remove the row. Treating
    that as a plan-bound persistence error escalated every such call, which is
    how an unbound write reached the wedge path at all.
    """
    from infra.database import get_db
    from infra.llm_usage import _turn_binding

    db = get_db()
    binding = _turn_binding(db, "some-thread", "a-turn-that-is-gone")

    assert binding == {
        "plan_id": None, "plan_node_id": None, "plan_attempt_id": None,
        "plan_commitment_id": None, "plan_usage_role": None,
    }
    db.close()


def test_the_bound_usage_error_never_escapes_any_of_its_call_sites():
    """The exception discriminates a failure class; it does not propagate.

    Every site that can raise it must also catch it. A new caller that forgets
    would reach a terminal path guarded only by try/finally, turning a lost
    ledger row into a turn the user never receives.
    """
    import ast
    import pathlib

    from infra import llm_usage

    backend = pathlib.Path(llm_usage.__file__).resolve().parents[1]
    callers = {
        "core/turns_util.py",
        "plans/usage_calls.py",
        "subagents/cc_event_usage.py",
        "subagents/codex_runtime_terminal.py",
    }
    found = set()
    for path in backend.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "PlanUsagePersistenceError" not in source:
            continue
        relative = str(path.relative_to(backend))
        if relative == "infra/llm_usage.py":
            continue
        found.add(relative)
        handlers = [
            handler
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if handler.type is not None
            and "PlanUsagePersistenceError" in ast.unparse(handler.type)
        ]
        assert handlers, f"{relative} references the error but never catches it"
        for handler in handlers:
            raises = [
                node for node in ast.walk(handler) if isinstance(node, ast.Raise)
            ]
            assert not raises, f"{relative} re-raises a bound-usage failure"

    assert found == callers


@pytest.mark.asyncio
async def test_cc_turn_total_without_a_measurement_is_the_bound_tasks_defect(
    isolated_db, monkeypatch,
):
    persist_turn("cc-session", "cc-unmeasured")
    _plan, node_id, _attempt, commitment_id, _role = bind_plan_turn("cc-session", "cc-unmeasured")

    await _finish_cc_turn(monkeypatch, "cc-unmeasured")

    assert [(d["usage_key"], d["reason"]) for d in _defect_payloads(node_id)] == [
        ("terminal", "measurement_missing"),
    ]
    assert _recording_failures(commitment_id) == 1


@pytest.mark.asyncio
async def test_cc_turn_total_that_cannot_persist_is_the_bound_tasks_defect(
    isolated_db, monkeypatch,
):
    persist_turn("cc-session", "cc-unwritable")
    _plan, node_id, _attempt, commitment_id, _role = bind_plan_turn("cc-session", "cc-unwritable")
    _refuse_writes(monkeypatch)

    await _finish_cc_turn(monkeypatch, "cc-unwritable")

    assert [(d["usage_key"], d["reason"]) for d in _defect_payloads(node_id)] == [
        ("terminal", "persist_failed"),
    ]
    assert _recording_failures(commitment_id) == 1


@pytest.mark.asyncio
async def test_codex_turn_total_without_a_measurement_is_the_bound_tasks_defect(
    isolated_db, monkeypatch,
):
    persist_turn("codex-session", "codex-unmeasured")
    _plan, node_id, _attempt, commitment_id, _role = bind_plan_turn(
        "codex-session", "codex-unmeasured",
    )

    await _finish_codex_turn(monkeypatch, "codex-unmeasured", measured=False)

    assert [(d["usage_key"], d["reason"]) for d in _defect_payloads(node_id)] == [
        ("terminal", "measurement_missing"),
    ]
    assert _recording_failures(commitment_id) == 1


@pytest.mark.asyncio
async def test_codex_turn_total_that_cannot_persist_is_the_bound_tasks_defect(
    isolated_db, monkeypatch,
):
    persist_turn("codex-session", "codex-unwritable")
    _plan, node_id, _attempt, commitment_id, _role = bind_plan_turn(
        "codex-session", "codex-unwritable",
    )
    _refuse_writes(monkeypatch)

    await _finish_codex_turn(monkeypatch, "codex-unwritable", measured=True)

    assert [(d["usage_key"], d["reason"]) for d in _defect_payloads(node_id)] == [
        ("terminal", "persist_failed"),
    ]
    assert _recording_failures(commitment_id) == 1


def test_failed_projection_leaves_receipt_for_exact_restart_recovery(db) -> None:
    """The independently failing ledger cannot erase measured Turn truth.

    The call does not raise: its receipt is durable and it is held for the
    Turn's terminal; restart reconciliation covers a process that exits first.
    """
    _record_through_projection(
        db,
        {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        fail_projection=True,
    )

    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
    pending = json.loads(
        db.execute(
            "SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0]
    )
    assert len(pending) == 1
    receipt_id = next(iter(pending))
    assert pending[receipt_id]["measured"]["total_tokens"] == 11

    assert reconcile_pending_turn_usage(db) == 1
    row = db.execute(
        """SELECT id,turn_id,total_tokens,ttft_ms,plan_id,plan_commitment_id
           FROM llm_usage"""
    ).fetchone()
    assert tuple(row) == (
        receipt_id,
        "turn-1",
        11,
        17,
        "plan-1",
        "commitment-1",
    )
    assert db.execute(
        "SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'"
    ).fetchone()[0] == "{}"

    assert reconcile_pending_turn_usage(db) == 0
    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1


def test_duplicate_turn_usage_replay_is_exactly_once(db) -> None:
    measured = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    _record_through_projection(db, measured)
    _record_through_projection(db, measured)

    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1
    assert db.execute("SELECT total_tokens FROM llm_usage").fetchone()[0] == 7
    assert db.execute(
        "SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'"
    ).fetchone()[0] == "{}"


def test_duplicate_usage_key_with_different_truth_fails_loudly(db) -> None:
    _record_through_projection(db, {"total_tokens": 7})

    with pytest.raises(UsageReceiptConflictError, match="different measurement"):
        _record_through_projection(db, {"total_tokens": 8})

    assert db.execute("SELECT total_tokens FROM llm_usage").fetchone()[0] == 7
    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1


def test_reconciliation_preserves_the_measurement_time_plan_binding(db) -> None:
    _record_through_projection(db, {"total_tokens": 9}, fail_projection=True)

    db.execute(
        """UPDATE active_turns
           SET plan_node_id='node-2',plan_attempt_id='attempt-2'
           WHERE id='turn-1'"""
    )
    db.commit()

    assert reconcile_pending_turn_usage(db) == 1
    row = db.execute(
        """SELECT plan_id,plan_node_id,plan_attempt_id,
                  plan_commitment_id,plan_usage_role FROM llm_usage"""
    ).fetchone()
    assert tuple(row) == (
        "plan-1",
        "node-1",
        "attempt-1",
        "commitment-1",
        "execution",
    )


def test_startup_reconciliation_emits_canonical_usage_event_once(
    db,
    monkeypatch,
) -> None:
    from infra import turn_usage_receipts

    _record_through_projection(
        db,
        {"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9},
        fail_projection=True,
    )
    emitted: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        turn_usage_receipts,
        "emit_llm_usage_recorded",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
        raising=False,
    )

    assert reconcile_pending_turn_usage(db) == 1
    assert emitted == [
        (
            ("thread-1", "test-model", {
                "prompt_tokens": 6,
                "completion_tokens": 3,
                "cache_creation_tokens": None,
                "cache_read_tokens": None,
                "total_tokens": 9,
                "cost_usd": None,
            }),
            {"source": "conversation:native", "ttft_ms": 17},
        )
    ]

    assert reconcile_pending_turn_usage(db) == 0
    assert len(emitted) == 1


def test_absent_provider_measurement_remains_unknown_after_handoff(db) -> None:
    _record_through_projection(db, None)

    row = db.execute(
        """SELECT prompt_tokens,completion_tokens,cache_creation_tokens,
                  cache_read_tokens,total_tokens,cost_usd
           FROM llm_usage"""
    ).fetchone()
    assert tuple(row) == (None, None, None, None, None, None)


def test_startup_reconciliation_has_one_hard_receipt_budget(
    db,
    monkeypatch,
) -> None:
    """One boot bounds query materialization and projection work together."""
    from infra import turn_usage_receipts

    warnings: list[str] = []
    monkeypatch.setattr(
        turn_usage_receipts,
        "logger",
        SimpleNamespace(
            warning=lambda message, *args: warnings.append(message.format(*args)),
        ),
    )
    for index in range(STARTUP_USAGE_RECEIPT_LIMIT + 1):
        stored = store_turn_usage_receipt(
            db,
            thread_id="thread-1",
            turn_id="turn-1",
            usage_key=f"provider-call:{index}",
            model="test-model",
            measured={"total_tokens": index},
            ttft_ms=None,
            source="conversation:native",
            now_iso=f"t{index + 1}",
        )
        assert stored is not None

    assert reconcile_pending_turn_usage(db) == STARTUP_USAGE_RECEIPT_LIMIT
    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == (
        STARTUP_USAGE_RECEIPT_LIMIT
    )
    pending = json.loads(
        db.execute(
            "SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0]
    )
    assert len(pending) == 1
    assert warnings == [
        "llm_usage: startup reconciliation reached receipt limit 256; "
        "any remaining receipts are retained for a later startup"
    ]

    assert reconcile_pending_turn_usage(db) == 1
    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == (
        STARTUP_USAGE_RECEIPT_LIMIT + 1
    )


def test_turn_audit_deletion_projects_a_pending_receipt_first(db) -> None:
    from infra.thread_driver import delete_thread_turns

    _record_through_projection(db, {"total_tokens": 13}, fail_projection=True)

    delete_thread_turns(db, "thread-1")
    db.commit()

    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    row = db.execute(
        "SELECT turn_id,total_tokens,plan_commitment_id FROM llm_usage"
    ).fetchone()
    assert tuple(row) == ("turn-1", 13, "commitment-1")


@pytest.mark.asyncio
async def test_claude_code_uses_one_stable_terminal_receipt_key(monkeypatch) -> None:
    from core.turn_core import TurnIdentity
    from infra.provider_usage import TokenUsage
    from subagents import cc_event_usage
    from subagents.cc_runtime_state import _Live

    captured: list[tuple[tuple, dict]] = []

    async def capture(fn, *args, **kwargs):
        captured.append((args, kwargs))

    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-turn", 1)
    live.turn_usage = TokenUsage(input_uncached=4, output=2)
    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)

    await cc_event_usage.record_terminal_llm_usage(
        "cc-thread", live, {"input_tokens": 4, "output_tokens": 2}
    )

    assert len(captured) == 1
    assert captured[0][1]["turn_id"] == "cc-turn"
    assert captured[0][1]["usage_key"] == "terminal"


@pytest.mark.asyncio
async def test_codex_uses_one_stable_terminal_receipt_key(monkeypatch) -> None:
    from infra.provider_usage import TokenUsage
    from subagents import codex_runtime_terminal

    captured: list[tuple[tuple, dict]] = []

    async def capture(fn, *args, **kwargs):
        captured.append((args, kwargs))

    live = SimpleNamespace(
        provider_reported_model="gpt-5-codex",
        harness="codex",
        corsia_turn=SimpleNamespace(turn_id="codex-turn"),
    )
    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)

    await codex_runtime_terminal.record_terminal_llm_usage(
        "codex-thread", live, TokenUsage(input_uncached=5, output=3)
    )

    assert len(captured) == 1
    assert captured[0][1]["turn_id"] == "codex-turn"
    assert captured[0][1]["usage_key"] == "terminal"


def test_a_receipted_turn_writes_no_total(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")
    _receipt("message:msg_01", 48, 12)

    assert _terminal(53, 13) is True

    assert _totals() == [60]


def test_a_turn_with_no_receipt_lands_whole(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")

    _terminal(48, 12)

    assert _totals() == [60]


def test_a_replayed_receipt_still_counts_as_receipted(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")
    for _replay in range(2):
        _receipt("message:msg_01", 48, 12)

    _terminal(53, 13)

    assert _totals() == [60]


def test_a_receipt_still_pending_projection_counts(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")
    db = get_db()
    try:
        store_turn_usage_receipt(
            db, thread_id="cc-thread", turn_id="cc-turn", usage_key="message:msg_09",
            model="opus", measured=_row(48, 12), ttft_ms=None, source=SOURCE, now_iso="t",
        )
    finally:
        db.close()

    _terminal(53, 13)

    assert _totals() == []


def test_a_turnless_total_lands_whole(isolated_db) -> None:
    _terminal(48, 12, turn_id=None)

    assert _totals() == [60]


def test_a_ledger_that_cannot_be_read_is_the_turns_defect(isolated_db, monkeypatch) -> None:
    """The report finds the Turn's task itself; the caller need not have read it."""
    persist_turn("cc-thread", "cc-turn")
    _plan_id, node_id, *_rest = bind_plan_turn("cc-thread", "cc-turn")

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(usage_calls, "holds_receipts", unreadable)
    assert _terminal(48, 12) is False

    assert _defect_event_names(node_id) == ["usage_recording_failed"]
    assert _totals() == []


def test_a_total_that_cannot_be_written_is_the_turns_defect(isolated_db, monkeypatch) -> None:
    persist_turn("cc-thread", "cc-turn")
    _plan_id, node_id, *_rest = bind_plan_turn("cc-thread", "cc-turn")

    def refuse(*_args, **_kwargs):
        raise RuntimeError("worker pool gone")

    monkeypatch.setattr(usage_calls, "record_llm_usage", refuse)
    assert _terminal(48, 12) is False

    assert _defect_event_names(node_id) == ["usage_recording_failed"]


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_defect_report_refuses_a_foreign_transaction(defect_case, existing_receipt):
    path, node_id = defect_case
    if existing_receipt:
        _report(node_id)
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='worker'")
    try:
        with pytest.raises(usage_calls.PlanUsagePersistenceError):
            _report(node_id)
        assert db.in_transaction
        assert _stored(path) == before
        db.commit()
        thread, events, receipts = _stored(path)
        assert thread == ("caller", before[0][1])
        assert (events, receipts) == before[1:]
    finally:
        db.rollback()
        db.close()


def test_defect_dedupe_excludes_a_competing_writer(defect_case, monkeypatch):
    path, node_id = defect_case
    protected = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "SELECT 1 FROM messages" in sql:
                try:
                    _writer_available(path)
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected.append(True)
                else:
                    protected.append(False)
            return super().execute(sql, *args, **kwargs)

    db = sqlite3.connect(str(path), factory=ObservedConnection)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    try:
        assert _report(node_id) is True
        assert protected and all(protected)
        _thread, events, receipts = _stored(path)
        assert len(events) == len(receipts) == 1
        assert events[0][0] == node_id
        assert "Usage recording failed (measurement_missing)" in receipts[0][0]
        _writer_available(path)
    finally:
        db.close()


@pytest.mark.parametrize("after_insert", [False, True])
def test_defect_receipt_failure_rolls_back_the_whole_report(
    defect_case, monkeypatch, after_insert,
):
    path, node_id = defect_case
    before = _stored(path)
    real_insert = queries.insert_message

    def fail_receipt(*args, **kwargs):
        if after_insert:
            real_insert(*args, **kwargs)
        raise RuntimeError("receipt rejected")

    with monkeypatch.context() as fault:
        fault.setattr(queries, "insert_message", fail_receipt)
        with pytest.raises(usage_calls.PlanUsagePersistenceError, match="receipt rejected"):
            _report(node_id)
    assert _stored(path) == before
    _writer_available(path)

    assert _report(node_id) is True
    assert _report(node_id) is True
    thread, events, receipts = _stored(path)
    assert len(events) == len(receipts) == 1
    assert thread[1] == before[0][1] + 1


def test_defect_commit_failure_preserves_prior_rows_and_releases_connection(
    defect_case, monkeypatch,
):
    path, node_id = defect_case
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("defect commit rejected")

    db = sqlite3.connect(str(path), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    try:
        with pytest.raises(usage_calls.PlanUsagePersistenceError, match="defect commit rejected"):
            _report(node_id)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            db.execute("SELECT 1")
        assert _stored(path) == before
        _writer_available(path)
    finally:
        db.close()


def test_retained_task_event_gets_its_missing_receipt_without_duplicate(defect_case):
    path, node_id = defect_case
    runner_events.record(
        node_id, "usage_recording_failed", provenance="plans.usage_calls",
        payload={
            "turn_id": "turn-1", "usage_key": "provider-call:1",
            "reason": "measurement_missing",
        },
    )
    before = _stored(path)
    assert len(before[1]) == 1 and before[2] == []
    assert _report(node_id) is True
    assert _report(node_id) is True
    thread, events, receipts = _stored(path)
    assert events == before[1]
    assert len(receipts) == 1
    assert thread[1] == before[0][1] + 1


@pytest.mark.parametrize("entry", ["direct", "missing_turn", "bound", "observed", "terminal"])
def test_usage_entries_preserve_an_unrelated_pooled_owner(usage_scope, entry):
    db = database.get_db()
    emitted = []
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='usage-project'")
        kwargs = {"source": "conversation", "_emit_product_event": lambda *a, **k: emitted.append(a)}
        if entry != "direct":
            kwargs.update(
                turn_id="missing" if entry == "missing_turn" else "usage-turn",
                usage_key="provider-call:1",
            )
        record = {
            "observed": usage_calls.record_observed_call_usage,
            "terminal": usage_calls.record_turn_fallback,
        }.get(entry, llm_usage.record_llm_usage)
        try:
            record("usage-thread", MODEL, SMALL_MEASURED, **kwargs)
        except llm_usage.PlanUsagePersistenceError:
            pass  # Bound accounting can report its refusal, but cannot own this write.
        assert db.in_transaction
        assert db.execute("SELECT name FROM projects WHERE id='usage-project'").fetchone()[0] == "Caller"
        assert _snapshot(usage_scope) == ("Original", 0, "{}")
        assert emitted == []
        db.rollback()
    finally:
        db.close()
    _writer_free(usage_scope)


def test_call_attribution_read_preserves_the_existing_write(usage_scope):
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='usage-project'")
        assert usage_calls.binding_at_call_start("usage-thread") is None
        assert db.in_transaction
        assert db.execute("SELECT name FROM projects WHERE id='usage-project'").fetchone()[0] == "Caller"
        assert _snapshot(usage_scope) == ("Original", 0, "{}")
        db.rollback()
    finally:
        db.close()


def test_direct_usage_and_recency_commit_before_telemetry(usage_scope):
    observed = []

    def emit(*args, **kwargs):
        observed.append((_snapshot(usage_scope), _writer_free(usage_scope), args, kwargs))

    llm_usage.record_llm_usage(
        "usage-thread", MODEL, SMALL_MEASURED, ttft_ms=19, _emit_product_event=emit,
    )
    assert len(observed) == 1
    snapshot, writer_free, args, kwargs = observed[0]
    assert snapshot == ("Original", 1, "{}")
    assert writer_free is True
    assert args == ("cost", "recorded")
    assert kwargs["properties"]["ttft_ms"] == 19
    with closing(sqlite3.connect(str(usage_scope))) as db:
        row = db.execute(
            "SELECT prompt_tokens,completion_tokens,total_tokens,cost_usd,ttft_ms,"
            "plan_id,created_at FROM llm_usage",
        ).fetchone()
        assert row[:6] == (5, 2, 7, None, 19, None)
        assert db.execute("SELECT last_used_at FROM models WHERE id=?", (MODEL,)).fetchone()[0] == row[6]


def test_partial_direct_write_rolls_back_usage_and_model_recency(usage_scope):
    with database.write_transaction(None, scope="usage_recency_fault") as db:
        prior = db.execute("SELECT last_used_at FROM models WHERE id=?", (MODEL,)).fetchone()[0]
        db.execute(
            "CREATE TRIGGER reject_usage_recency AFTER UPDATE OF last_used_at ON models "
            "BEGIN SELECT RAISE(FAIL, 'recency rejected'); END",
        )
    emitted = []
    llm_usage.record_llm_usage(
        "usage-thread", MODEL, SMALL_MEASURED,
        _emit_product_event=lambda *a, **k: emitted.append(a),
    )
    assert _snapshot(usage_scope) == ("Original", 0, "{}")
    assert emitted == []
    with closing(sqlite3.connect(str(usage_scope))) as db:
        assert db.execute("SELECT last_used_at FROM models WHERE id=?", (MODEL,)).fetchone()[0] == prior
    _writer_free(usage_scope)


def test_failed_projection_retains_a_separately_committed_turn_receipt(usage_scope):
    with database.write_transaction(None, scope="usage_projection_fault") as db:
        db.execute(
            "CREATE TRIGGER reject_usage_projection BEFORE INSERT ON llm_usage "
            "BEGIN SELECT RAISE(FAIL, 'projection rejected'); END",
        )
    emitted = []
    # Held for the Turn's terminal rather than raised; the receipt is durable now.
    llm_usage.record_llm_usage(
        "usage-thread", MODEL, SMALL_MEASURED, turn_id="usage-turn", usage_key="provider-call:1",
        _emit_product_event=lambda *a, **k: emitted.append(a),
    )
    name, count, raw = _snapshot(usage_scope)
    assert (name, count) == ("Original", 0)
    receipts = json.loads(raw)
    assert len(receipts) == 1
    receipt = next(iter(receipts.values()))
    assert receipt["measured"]["total_tokens"] == 7
    assert receipt["binding"]["plan_commitment_id"] == "usage-commitment"
    assert emitted == []
    _writer_free(usage_scope)


def test_a_failed_bound_write_leaves_a_visible_defect(isolated_db, monkeypatch):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)

        def refuse(*_args, **_kwargs):
            raise PlanUsagePersistenceError("plan-bound usage receipt could not be persisted")

        monkeypatch.setattr(usage_calls, "record_llm_usage", refuse)
        # Loud, never blocking: the call returns; a retry of the same failure
        # is the same defect, not a second one.
        for _attempt in range(2):
            usage_calls.record_call_usage(
                "worker", "test-model", MEASURED, source="conversation",
                turn_id="turn-1", usage_key="provider-call:1",
                binding=binding_at_call_start("worker"),
            )

        defects = _defect_payloads_in_db(db, node.id)
        assert len(defects) == 1
        assert defects[0]["turn_id"] == "turn-1"
        assert defects[0]["usage_key"] == "provider-call:1"
        assert defects[0]["reason"] == "persist_failed"
        assert len(_receipts(db)) == 1
        assert "Usage recording failed" in _receipts(db)[0]
    finally:
        db.close()


def test_an_unexpected_failure_is_the_same_visible_defect(isolated_db, monkeypatch):
    """Whatever failed, the call's receipt is missing and the task says so."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)

        def explode(*_args, **_kwargs):
            raise RuntimeError("worker pool gone")

        monkeypatch.setattr(usage_calls, "record_llm_usage", explode)
        recorded = usage_calls.record_call_usage(
            "worker", "test-model", MEASURED, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )

        assert recorded is False
        defects = _defect_payloads_in_db(db, node.id)
        assert [d["reason"] for d in defects] == ["persist_failed"]
        assert "worker pool gone" in _receipts(db)[0]
    finally:
        db.close()


def test_a_call_the_ledger_cannot_take_is_reported_on_the_turns_task(isolated_db):
    """A harness with no model to name still leaves the loss on the Turn's task."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)

        usage_calls.report_unrecordable_call(
            "worker", turn_id="turn-1", usage_key="message:m1",
            reason="model_unknown", detail="the provider named no model for this call",
        )

        defects = _defect_payloads_in_db(db, node.id)
        assert [d["reason"] for d in defects] == ["model_unknown"]
        assert "named no model" in _receipts(db)[0]
    finally:
        db.close()


def test_a_bound_call_without_a_measurement_is_a_defect_and_an_unknown_row(isolated_db):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)

        usage_calls.record_call_usage(
            "worker", "test-model", {}, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )

        row = db.execute(
            "SELECT total_tokens,plan_node_id FROM llm_usage WHERE turn_id='turn-1'"
        ).fetchone()
        assert (row["total_tokens"], row["plan_node_id"]) == (None, node.id)
        assert [d["reason"] for d in _defect_payloads_in_db(db, node.id)] == ["measurement_missing"]
        assert len(_receipts(db)) == 1
    finally:
        db.close()


def test_an_unbound_best_effort_failure_writes_no_defect(isolated_db, monkeypatch):
    """A Turn bound to no task has nothing to mark: loud in the log, no defect invented."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        def refuse(*_args, **_kwargs):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(usage_calls, "record_llm_usage", refuse)
        recorded = usage_calls.record_call_usage(
            "worker", "test-model", MEASURED, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )

        assert recorded is False
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE event='usage_recording_failed'"
        ).fetchone()[0] == 0
        assert _receipts(db) == []
        # Without even a Turn, the failure is the caller's to see.
        with pytest.raises(RuntimeError):
            usage_calls.record_call_usage(
                "worker", "test-model", MEASURED, source="conversation",
                turn_id=None, usage_key=None, binding=None,
            )
    finally:
        db.close()


def test_commitment_cost_counts_recording_failures(isolated_db, monkeypatch):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)

        def refuse(*_args, **_kwargs):
            raise PlanUsagePersistenceError("plan-bound usage receipt could not be persisted")

        monkeypatch.setattr(usage_calls, "record_llm_usage", refuse)
        usage_calls.record_call_usage(
            "worker", "test-model", MEASURED, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )

        cost = commitment_cost.project(node.commitment_id, db=db)
        assert cost["usage_recording_failures"] == 1
    finally:
        db.close()


def test_an_unreadable_attribution_is_a_visible_defect_not_a_guess(isolated_db, monkeypatch):
    """The row is written without a task, and the owner is told why."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        _selected_task(db)
        monkeypatch.setattr(
            usage_calls, "call_binding",
            lambda _thread_id, *, db=None: (_ for _ in ()).throw(RuntimeError("read failed")),
        )
        captured = binding_at_call_start("worker")
        assert captured == usage_calls.UNATTRIBUTED

        for _ in range(2):
            usage_calls.record_call_usage(
                "worker", "test-model", {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                source="conversation", turn_id="turn-1", usage_key="provider-call:1",
                binding=captured,
            )

        row = db.execute(
            "SELECT total_tokens,plan_node_id FROM llm_usage WHERE turn_id='turn-1'"
        ).fetchone()
        assert (row["total_tokens"], row["plan_node_id"]) == (4, None)
        receipts = [c for c in _receipts(db) if "attribution_unreadable" in c]
        assert len(receipts) == 1
    finally:
        db.close()


def test_a_defect_report_that_fails_once_completes_on_the_next_call(isolated_db, monkeypatch):
    """The owner receipt is what makes the report done; a retry writes it."""
    from infra import queries

    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)
        real_insert = queries.insert_message
        failures = {"left": 1}

        def flaky(*args, **kwargs):
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("receipt write failed")
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(queries, "insert_message", flaky)
        record = lambda: usage_calls.record_call_usage(  # noqa: E731
            "worker", "test-model", {}, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )
        # The first report fails loudly (the contract error the callers log);
        # the second writes the receipt the first could not.
        with pytest.raises(usage_calls.PlanUsagePersistenceError):
            record()
        record()

        assert [d["reason"] for d in _defect_payloads_in_db(db, node.id)] == ["measurement_missing"]
        assert len(_receipts(db)) == 1
    finally:
        db.close()


def test_a_partial_measurement_is_not_a_measurement(isolated_db):
    """Knowing one part and not the total leaves the total unknown: a defect."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        node = _selected_task(db)
        usage_calls.record_call_usage(
            "worker", "test-model",
            {"prompt_tokens": 10, "completion_tokens": None, "total_tokens": None},
            source="conversation", turn_id="turn-1", usage_key="provider-call:1",
            binding=binding_at_call_start("worker"),
        )
        assert [d["reason"] for d in _defect_payloads_in_db(db, node.id)] == ["measurement_missing"]
    finally:
        db.close()


def test_a_defect_report_that_cannot_be_written_is_the_contract_error_not_a_crash(isolated_db, monkeypatch):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        _selected_task(db)
        captured = binding_at_call_start("worker")
    finally:
        db.close()

    def no_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "get_db", no_db)
    monkeypatch.setattr(usage_calls, "record_llm_usage", lambda *a, **k: None)
    with pytest.raises(usage_calls.PlanUsagePersistenceError, match="could not be reported"):
        usage_calls.record_call_usage(
            "worker", "test-model", {}, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1", binding=captured,
        )


def test_the_same_call_key_on_a_later_turn_is_its_own_defect(isolated_db):
    """provider-call:1 repeats every Turn; a defect is one (turn, call, reason)."""
    db = get_db()
    try:
        node = _selected_task(db)
        for turn in ("turn-1", "turn-2"):
            if turn == "turn-2":
                # the first Turn has ended; only its ledger rows and receipts remain
                db.execute("DELETE FROM active_turns WHERE id='turn-1'")
                db.commit()
            persist_turn("worker", turn)
            usage_calls.record_call_usage(
                "worker", "test-model", {}, source="conversation",
                turn_id=turn, usage_key="provider-call:1",
                binding=binding_at_call_start("worker"),
            )
        assert [d["reason"] for d in _defect_payloads_in_db(db, node.id)] == ["measurement_missing"] * 2
        assert len(_receipts(db)) == 2
    finally:
        db.close()


@pytest.mark.parametrize("source", ["external_harness:claude-code", "external_harness:codex"])
def test_held_per_call_receipts_suppress_the_terminal_fallback(isolated_db, source):
    from plans.usage_calls import record_observed_call_usage, record_turn_fallback

    persist_settleable_turn("worker", "turn-1")
    calls = {"call:1": (40, 12), "call:2": (5, 1)}
    with _receipt_store_locked():
        for key, (prompt, completion) in calls.items():
            record_observed_call_usage(
                "worker", "test-model",
                {"prompt_tokens": prompt, "completion_tokens": completion, "cost_usd": None},
                source=source, turn_id="turn-1", usage_key=key,
            )
    record_turn_fallback(
        "worker", "test-model",
        {"prompt_tokens": 45, "completion_tokens": 13, "cost_usd": None},
        source=source, turn_id="turn-1",
    )

    finish_turn(
        "turn-1", outcome="completed", ended_at="t9",
        successor_turn_id="turn-1-next", boot_epoch="test-boot",
    )

    assert _token_pairs() == [(5, 1), (40, 12)]


@pytest.mark.asyncio
async def test_claude_code_event_pump_survives_an_unrecordable_usage_report(monkeypatch):
    from core.turn_core import TurnIdentity
    from subagents import cc_event_usage
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    monkeypatch.setattr(cc_event_usage, "run_in_db", _refuse_defect_report())
    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-turn", 1)
    live.turn_active = True
    on_event = _make_on_event(live, "cc-thread", lambda *_args: None, db=object())

    result = await on_event({
        "kind": "final_call_usage", "message_id": "msg_01", "model": "claude-opus-4-1",
        "usage": {"input_tokens": 40, "output_tokens": 12},
    })

    assert result is None
    assert live.turn_usage is not None


@pytest.mark.asyncio
async def test_codex_event_dispatch_survives_an_unrecordable_usage_report(monkeypatch):
    from subagents import codex_runtime_events, codex_usage_calls

    monkeypatch.setattr(codex_usage_calls, "run_in_db", _refuse_defect_report())
    live = SimpleNamespace(
        provider_reported_model="gpt-5-codex",
        harness="codex",
        corsia_turn=SimpleNamespace(turn_id="codex-turn"),
    )
    ctx = SimpleNamespace(thread_id="codex-thread", live=live)

    await codex_runtime_events._dispatch_on_event(ctx, {
        "kind": "call_usage", "turn_id": "provider-turn", "ordinal": 1,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    })


def test_a_native_call_in_a_lock_window_lands_when_its_turn_settles(isolated_db):
    from core.turns_util import _log_llm_usage

    persist_settleable_turn("worker", "turn-1")
    with _lock_window("store_turn_usage_receipt"):
        _log_llm_usage(
            "worker", "test-model", MEASURED, ttft_ms=7,
            turn_id="turn-1", usage_key="provider-call:1",
        )
    assert _retained_turn_usage_rows() == []

    assert _finish().result is TerminalResult.APPLIED

    [row] = _retained_turn_usage_rows()
    assert row["id"] == _receipt_id("turn-1", "provider-call:1")
    assert (row["turn_id"], row["total_tokens"], row["ttft_ms"]) == ("turn-1", 12, 7)
    [turn] = _query("SELECT status,usage_receipts_json FROM active_turns WHERE id='turn-1'")
    assert tuple(turn) == ("completed", "{}")


def test_a_real_lock_holder_cannot_lose_the_call(isolated_db, monkeypatch):
    monkeypatch.setattr(database, "CONNECTION_BUSY_TIMEOUT_MS", 50)
    persist_settleable_turn("worker", "turn-1")
    holder = sqlite3.connect(str(isolated_db))
    try:
        holder.execute("BEGIN IMMEDIATE")
        _record_worker_usage()
    finally:
        holder.rollback()
        holder.close()
    assert _retained_turn_usage_rows() == []

    _finish()

    assert [(r["id"], r["total_tokens"]) for r in _retained_turn_usage_rows()] == [
        (_receipt_id("turn-1", "provider-call:1"), 12),
    ]


def test_a_plan_bound_call_in_a_lock_window_bills_its_task_and_is_no_defect(isolated_db):
    from plans import commitment_cost
    from plans.usage_calls import binding_at_call_start, record_call_usage

    first, second = _plan_bound_turn()
    binding = binding_at_call_start("worker")
    with _lock_window("store_turn_usage_receipt"):
        recorded = record_call_usage(
            "worker", "test-model", MEASURED, source="conversation",
            turn_id="turn-1", usage_key="provider-call:1", binding=binding,
        )
    assert recorded is True
    assert _defect_reasons() == []
    held = commitment_cost.project(second.commitment_id)
    assert (held["tokens"]["known"], held["tokens"]["unknown_rows"]) == (None, 1)

    _finish()

    [row] = _retained_turn_usage_rows()
    assert row["plan_node_id"] == second.id
    settled = commitment_cost.project(second.commitment_id)
    assert (settled["tokens"]["known"], settled["tokens"]["unknown_rows"]) == (12, 0)
    assert settled["usage_recording_failures"] == 0
    assert commitment_cost.project(first.commitment_id)["tokens"]["known"] is None
    assert _query(
        "SELECT COUNT(*) FROM messages WHERE metadata LIKE '%usage.recording_failed%'"
    )[0][0] == 0


def test_an_unreadable_turn_binding_holds_the_call_instead_of_dropping_it(
    isolated_db, monkeypatch,
):
    import infra.llm_usage as llm_usage

    persist_settleable_turn("worker", "turn-1")

    def locked(*_args):
        raise sqlite3.OperationalError("database is locked")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(llm_usage, "_turn_binding", locked)
        _record_worker_usage()

    _finish()

    assert [r["total_tokens"] for r in _retained_turn_usage_rows()] == [12]


def test_a_call_with_no_turn_lands_at_its_threads_next_terminal(isolated_db):
    persist_settleable_turn("worker", "turn-1")
    with _lock_window("direct_llm_usage"):
        _record_worker_usage(turn_id=None, key=None)
    assert _retained_turn_usage_rows() == []

    _finish()

    [row] = _retained_turn_usage_rows()
    assert (row["turn_id"], row["plan_id"], row["total_tokens"]) == (None, None, 12)


def test_a_call_whose_turn_row_is_gone_lands_unbound(isolated_db):
    persist_settleable_turn("worker", "turn-1")
    with _lock_window():
        _record_worker_usage(turn_id="vanished", key="provider-call:1")

    _finish()

    [row] = _retained_turn_usage_rows()
    assert (row["turn_id"], row["plan_id"], row["total_tokens"]) == ("vanished", None, 12)


def test_a_bad_entry_is_a_defect_only_when_the_terminal_cannot_store_it_either(isolated_db):
    from plans.usage_calls import binding_at_call_start, record_call_usage

    _first, second = _plan_bound_turn()
    binding = binding_at_call_start("worker")
    with _lock_window("store_turn_usage_receipt"):
        for key, total in (("provider-call:1", 12), ("provider-call:2", 99)):
            record_call_usage(
                "worker", "test-model", {**MEASURED, "total_tokens": total},
                source="conversation", turn_id="turn-1", usage_key=key, binding=binding,
            )
    with database.write_transaction(None, scope="reject_usage_row") as db:
        db.execute(
            "CREATE TRIGGER reject_usage_row BEFORE INSERT ON llm_usage "
            "WHEN NEW.total_tokens=99 BEGIN SELECT RAISE(FAIL, 'row rejected'); END"
        )

    assert _finish().result is TerminalResult.APPLIED

    assert [r["total_tokens"] for r in _retained_turn_usage_rows()] == [12]
    assert _query("SELECT status FROM active_turns WHERE id='turn-1'")[0][0] == "completed"
    assert _defect_reasons() == ["persist_failed"]

    with database.write_transaction(None, scope="accept_usage_row") as db:
        db.execute("DROP TRIGGER reject_usage_row")
    persist_settleable_turn("worker", "turn-2")
    _finish("turn-2")

    assert [(r["total_tokens"], r["plan_node_id"]) for r in _retained_turn_usage_rows()] == [
        (12, second.id), (99, second.id),
    ]
    assert _defect_reasons() == ["persist_failed"]


def test_a_failed_projection_is_completed_by_the_turns_terminal(isolated_db):
    persist_settleable_turn("worker", "turn-1")
    with database.write_transaction(None, scope="reject_projection") as db:
        db.execute(
            "CREATE TRIGGER reject_projection BEFORE INSERT ON llm_usage "
            "BEGIN SELECT RAISE(FAIL, 'projection rejected'); END"
        )
    _record_worker_usage()
    with database.write_transaction(None, scope="accept_projection") as db:
        db.execute("DROP TRIGGER reject_projection")
    assert _retained_turn_usage_rows() == []

    _finish()

    assert [r["id"] for r in _retained_turn_usage_rows()] == [_receipt_id("turn-1", "provider-call:1")]
    assert _query("SELECT usage_receipts_json FROM active_turns WHERE id='turn-1'")[0][0] == "{}"


def test_a_terminal_that_fails_to_commit_keeps_the_held_call(isolated_db, monkeypatch):
    import infra.thread_driver as thread_driver

    persist_settleable_turn("worker", "turn-1")
    with _lock_window():
        _record_worker_usage()

    def refuse(*_args, **_kwargs):
        raise RuntimeError("successor claim failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(thread_driver, "start_idle_turn", refuse)
        with pytest.raises(RuntimeError, match="successor claim failed"):
            _finish()
    assert _retained_turn_usage_rows() == []
    assert _query("SELECT status FROM active_turns WHERE id='turn-1'")[0][0] == "running"

    assert _finish().result is TerminalResult.APPLIED

    assert [r["total_tokens"] for r in _retained_turn_usage_rows()] == [12]


def test_replays_and_later_successes_of_a_held_call_are_one_row(isolated_db):
    persist_settleable_turn("worker", "turn-1")
    with _lock_window():
        _record_worker_usage()
        _record_worker_usage()
    _finish()
    assert [r["total_tokens"] for r in _retained_turn_usage_rows()] == [12]

    persist_settleable_turn("worker", "turn-2")
    with _lock_window():
        _record_worker_usage(turn_id="turn-2")
    _record_worker_usage(turn_id="turn-2")
    _finish("turn-2")

    assert [r["id"] for r in _retained_turn_usage_rows()] == sorted(
        [_receipt_id("turn-1", "provider-call:1"), _receipt_id("turn-2", "provider-call:1")]
    )


def test_a_write_that_committed_before_raising_is_one_row(isolated_db, monkeypatch):
    import infra.llm_usage as llm_usage

    real_store, real_direct = llm_usage.store_turn_usage_receipt, llm_usage._write_direct_usage

    def store_then_raise(*args, **kwargs):
        real_store(*args, **kwargs)
        raise sqlite3.OperationalError("disk I/O error")

    def direct_then_raise(*args, **kwargs):
        real_direct(*args, **kwargs)
        raise sqlite3.OperationalError("disk I/O error")

    persist_settleable_turn("worker", "turn-1")
    monkeypatch.setattr(llm_usage, "store_turn_usage_receipt", store_then_raise)
    monkeypatch.setattr(llm_usage, "_write_direct_usage", direct_then_raise)
    _record_worker_usage()
    _record_worker_usage(turn_id=None, key=None, usage={**MEASURED, "total_tokens": 30})

    _finish()

    assert [(r["turn_id"], r["total_tokens"]) for r in _retained_turn_usage_rows()] == [("turn-1", 12), (None, 30)]


def test_a_terminal_with_nothing_held_runs_no_usage_sql(isolated_db):
    """The ordinary terminal is unchanged: no query, savepoint or write for usage."""
    persist_settleable_turn("worker", "turn-1")
    db = get_db()
    statements: list[str] = []
    try:
        db.set_trace_callback(statements.append)
        finish_turn(
            "turn-1", outcome="completed", ended_at="t9",
            successor_turn_id="turn-1-next", boot_epoch="test-boot", db=db,
        )
    finally:
        db.set_trace_callback(None)
        db.close()
    assert statements
    assert not [s for s in statements if "llm_usage" in s or "usage_receipts_json" in s]
