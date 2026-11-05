"""The account-hold projection reads each global fact once, and nothing else changes.

``refresh_account_holds`` walks the affected set and calls the retained single-thread
owner ``refresh_thread_holds`` for each subject, inside the caller's writer transaction.
Every thread re-reads the same project policy, project row, account rows and model rows
through the same canonical readers, so the projection opens one scoped read memo
(``connections.fact_memo``) around the walk and the fan-out. No evaluator changes: the
readers answer their first read while the scope is open and behave exactly as before
outside it, and thread-specific reads (a thread's own scope, bindings, pending account
and queued head) are never memoized.

Pins, with literal expected rows: the binding shapes, a thread whose ``project_id`` is
``''``, a rollover policy, the memoized readers' execution counts (flat while the scope
is open and scaling again with the scope disabled, so the ceiling is load-bearing), an
arm with the scope disabled writing identical rows, every answer the scope stores
digesting to what it stored around a whole projection for each vendor, the scope closed
when the projection returns, no thread or task hop inside it, and the ``wake=True``
after-commit effect running outside the scope.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import sqlite3
import threading

import pytest

from vendor_account_fixtures import (  # noqa: F401
    _accept, _held, _ready, accounts, set_legacy_policy, vendors,
)

HOLD_ROWS_SQL = ("SELECT thread_id,provider,account_id,reason,updated_at "
                 "FROM thread_vendor_account_holds ORDER BY thread_id,provider")
QUEUED_ROWS_SQL = ("SELECT id,write_outcome,delivery_error FROM messages "
                   "WHERE input_order IS NOT NULL ORDER BY id")
FROZEN_NOW = "2026-10-06T00:00:00+00:00"
#: Distinct facts one projection's scope stores, per vendor, in the fixture below.
# The model row's availability is derived from the vendor's accounts, so the account list and
# its projection are two of the shared answers.
EXPECTED_ANSWERS = {"claude-code": 7, "codex": 7}


def _thread(db, thread_id, *, project="destination-project", model="codex/test"):
    db.execute("INSERT OR REPLACE INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES (?,'user',?,?,'t','t')", (thread_id, model, project))


def _bind(db, thread_id, provider, account_id, pending=None):
    db.execute("INSERT OR REPLACE INTO thread_vendor_accounts(thread_id,provider,account_id,pending_account_id) "
               "VALUES (?,?,?,?)", (thread_id, provider, account_id, pending))


def _rows(db, sql):
    return [tuple(row) for row in db.execute(sql)]


def _digest(db):
    """Canonical digest of every hold row and every queued input's hold fields."""
    hasher = hashlib.sha256()
    for sql in (HOLD_ROWS_SQL, QUEUED_ROWS_SQL):
        hasher.update(sql.encode() + b"\n")
        for row in _rows(db, sql):
            hasher.update(repr(row).encode() + b"\n")
    return hasher.hexdigest()


def _freeze(monkeypatch):
    """One clock, so a parity digest can include ``updated_at``."""
    monkeypatch.setattr("infra.time.utc_now_iso", lambda: FROZEN_NOW)
    monkeypatch.setattr("connections.account_holds.utc_now_iso", lambda: FROZEN_NOW)


def _copy(db, path):
    """An independent connection holding exactly this state, for a second arm."""
    target = sqlite3.connect(str(path))
    target.row_factory = sqlite3.Row
    db.backup(target)
    return target


def _project(db, account_id, *, wake=False, scope="test_account_hold_projection"):
    """The production entry point, in the caller's writer transaction."""
    from connections.account_holds import refresh_account_holds
    from infra.database import write_transaction

    with write_transaction(db, scope=scope):
        refresh_account_holds(account_id, wake=wake, db=db)


def _seed_population(db, account_id, count, *, start=0, provider="codex", model="codex/test"):
    for index in range(start, start + count):
        thread_id = f"bulk-{index}"
        _thread(db, thread_id, model=model)
        _bind(db, thread_id, provider, account_id)


def _count_reads(monkeypatch):
    """Every wrapped-reader call, and the ones the memo had to read for real."""
    from connections import fact_memo

    calls: list[tuple] = []
    reads: list[tuple] = []
    real = fact_memo.memoized

    def counting(key, read):
        scope = fact_memo._answers.get()
        calls.append(key)
        if scope is None or key not in scope:
            reads.append(key)
        return real(key, read)

    monkeypatch.setattr(fact_memo, "memoized", counting)
    return calls, reads


def _family(reads, name):
    return [key for key in reads if key[1].rpartition(".")[2] == name]


def _without_scope(monkeypatch):
    """Disable the memo: the same code path, every reader re-read per thread."""
    from connections import fact_memo

    real = fact_memo.fact_reads

    @contextlib.contextmanager
    def no_scope():
        yield

    monkeypatch.setattr(fact_memo, "fact_reads", no_scope)
    return real


def _answer_digest(answer) -> str:
    """One canonical digest of a memo answer, so an in-place change cannot hide."""
    return json.dumps(answer, sort_keys=True, default=str)


def _digest_answers(monkeypatch):
    """Digest every answer the scope stores, held against the object the scope keeps.

    The memo shares one answer per fact with every caller in the projection, so a
    digest taken as the answer is stored and re-taken when the projection returns
    fails the moment any in-scope consumer changes a shared answer in place.
    """
    from connections import fact_memo

    real = fact_memo.memoized
    stored: dict[tuple, tuple[str, object]] = {}

    def digesting(key, read):
        answer = real(key, read)
        scope = fact_memo._answers.get()
        if key not in stored and scope is not None and scope.get(key) is answer:
            stored[key] = (_answer_digest(answer), answer)
        return answer

    monkeypatch.setattr(fact_memo, "memoized", digesting)
    return stored


def _ready_vendor(db, account, provider, model):
    """The same readiness ``_ready`` gives a codex account, for either vendor."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    assert receipt is not None
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"resolved_identity": "synthetic"}}, db=db)
    db.commit()


def _facts(db, project, provider="codex", *, model="codex/test"):
    """One read of every wrapped fact, outside any scope."""
    from connections.account_policy import project_exists
    from connections.account_rollover import rollover_destination
    from connections.provider_policy import effective_policy
    from connections.vendor_accounts import get_account, list_accounts
    from infra.vendor_turn_binding import model_vendor
    from llm.account_catalog import account_model_status
    from llm.model_status import harness_base_row

    row = harness_base_row(model, db=db)
    identity = list_accounts(provider=provider, db=db)[0]["id"]
    account = get_account(identity, db=db)
    return {
        "policy": effective_policy(project, provider, db=db),
        "exists": project_exists(project, db=db),
        "row": row,
        "vendor": model_vendor(model, db=db),
        "account": account,
        "account_status": account_model_status(account, row, project_ids=[project], db=db),
        "rollover": rollover_destination(provider, None, row, project, db=db),
    }


def test_memo_rejects_an_unhashable_reader_argument_inside_a_scope(accounts):
    """A dict cannot silently disappear from the mechanical memo key."""
    from connections import fact_memo

    db, _default, _second, _third = accounts
    calls = []

    def reader(row, *, db):
        calls.append((row, db))
        return {"id": row["id"], "state": "signed_in"}

    wrapped = fact_memo.memoized_reader(reader)
    assert wrapped({"id": "literal-account"}, db=db) == {"id": "literal-account", "state": "signed_in"}
    with fact_memo.fact_reads(), pytest.raises(TypeError, match="must be hashable"):
        wrapped({"id": "literal-account"}, db=db)
    assert len(calls) == 1


class _NoClose:
    """A fixture connection exposed as db=None's fresh reader without closing it."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def test_db_none_bypasses_the_scope_and_stores_no_answer(accounts, monkeypatch):
    """A reader that opens its own connection cannot cache a pre-commit snapshot."""
    from connections import fact_memo
    from connections.vendor_accounts import get_account

    db, _default, second, _third = accounts
    monkeypatch.setattr("connections.vendor_accounts.get_db", lambda: _NoClose(db))
    with fact_memo.fact_reads():
        scoped = get_account(second["id"], db=db)
        bypass = get_account(second["id"], db=None)
        scope = fact_memo._answers.get()
        assert scoped["name"] == bypass["name"] == "Second"
        assert scoped is not bypass
        assert len(scope) == 1


def test_different_connections_do_not_share_one_memo_answer(accounts, tmp_path):
    """Connection identity is part of the mechanical key, even for the same account id."""
    from connections import fact_memo
    from connections.vendor_accounts import get_account

    db, _default, second, _third = accounts
    other = _copy(db, tmp_path / "other-account-row.db")
    try:
        other.execute("UPDATE vendor_accounts SET name='Second in other connection' WHERE id=?", (second["id"],))
        other.commit()
        with fact_memo.fact_reads():
            first = get_account(second["id"], db=db)
            second_copy = get_account(second["id"], db=other)
            scope = fact_memo._answers.get()
            assert first["name"] == "Second"
            assert second_copy["name"] == "Second in other connection"
            assert len(scope) == 2
    finally:
        other.close()


def test_hold_rows_are_literal_for_each_binding_shape(accounts, monkeypatch):
    """A bound thread holds while its account is unusable, and clears once it is ready."""
    db, _default, second, third = accounts
    _freeze(monkeypatch)
    _thread(db, "bound-signed-out")
    _bind(db, "bound-signed-out", "codex", second["id"])
    _thread(db, "bound-other-account")
    _bind(db, "bound-other-account", "codex", third["id"])
    db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (second["id"],))
    db.commit()

    _project(db, second["id"])
    assert _rows(db, HOLD_ROWS_SQL) == [
        ("bound-signed-out", "codex", second["id"], "account_signed_out", FROZEN_NOW)]

    db.execute("UPDATE vendor_accounts SET state='signed_in' WHERE id=?", (second["id"],))
    db.commit()
    _ready(db, second)
    _project(db, second["id"])
    assert _rows(db, HOLD_ROWS_SQL) == []


def test_queued_input_holds_literally_with_its_claim_reason(accounts, monkeypatch):
    """An unbound thread's queued input holds on the message, with no binding row in play."""
    db, _default, second, _third = accounts
    _freeze(monkeypatch)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]],
                      default_account_id=second["id"], db=db)
    db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (second["id"],))
    _accept(db, "queued-brief")
    _held(db, "queued-brief")
    db.commit()

    _project(db, second["id"])
    assert _rows(db, HOLD_ROWS_SQL) == []
    assert _rows(db, QUEUED_ROWS_SQL) == [("queued-brief", "proved_no_write", "account_signed_out")]


def test_empty_project_id_thread_holds_literally(accounts, monkeypatch):
    """A thread whose ``project_id`` is ``''`` is judged by the one project read both callers share."""
    from connections.account_holds import affected_thread_ids
    from connections.account_policy import project_exists

    db, _default, second, _third = accounts
    _freeze(monkeypatch)
    _thread(db, "bound-signed-out")
    _bind(db, "bound-signed-out", "codex", second["id"])
    _thread(db, "empty-project", project="")
    _bind(db, "empty-project", "codex", second["id"])
    db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (second["id"],))
    db.commit()

    # The canonical selection reads ``''`` as no project at all, so the thread is no subject
    # of this change and keeps whatever holds it had (here: none), exactly as before.
    assert affected_thread_ids(second["id"], db=db) == ["bound-signed-out"]
    assert project_exists("", db=db) is False
    _calls, reads = _count_reads(monkeypatch)
    _project(db, second["id"])
    assert _rows(db, HOLD_ROWS_SQL) == [
        ("bound-signed-out", "codex", second["id"], "account_signed_out", FROZEN_NOW)]
    assert len(_family(reads, "project_exists")) == 2


def _discover(db, provider, account, model="codex/test"):
    """A current login whose discovery record names this model, as the account suites do."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    return receipt


def test_rollover_policy_keeps_the_canonical_destination(accounts, monkeypatch):
    """A rollover policy keeps its canonical destination while the inner facts are memoized."""
    from connections import harness_usage
    from connections.account_holds import affected_thread_ids
    from connections.account_receipts import capture_receipt
    from connections.account_rollover import rollover_destination
    from connections.vendor_accounts import get_account
    from llm.account_catalog import account_model_status
    from llm.model_status import harness_base_row

    db, _default, second, third = accounts
    _freeze(monkeypatch)
    # The source account is limited on its current login; the destination has an allowed one.
    harness_usage.record_limited(provider="codex", receipt=capture_receipt("codex", account_id=second["id"], db=db),
                                 reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                                 source="rate_limit_update")
    harness_usage.record_allowed(provider="codex", source="discovery",
                                 receipt=capture_receipt("codex", account_id=third["id"], db=db))
    _discover(db, "codex", second)
    _discover(db, "codex", third)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"], third["id"]],
                      default_account_id=second["id"], switch_on_limit=True, db=db)
    _seed_population(db, second["id"], 5, start=0)
    db.commit()

    row = harness_base_row("codex/test", db=db)
    assert account_model_status(get_account(second["id"], db=db), row,
                                project_ids=["destination-project"], db=db)["status_reason"] == "usage_limited"
    assert rollover_destination("codex", second["id"], row, "destination-project", db=db) == third["id"]
    # Every seeded thread is bound to the limited account, so the destination project owns them.
    assert affected_thread_ids(second["id"], db=db) == []
    assert affected_thread_ids(third["id"], db=db) == [f"bulk-{index}" for index in range(5)]

    _project(db, third["id"])
    assert _rows(db, HOLD_ROWS_SQL) == []


def test_memoized_readers_run_once_however_many_threads_are_affected(accounts, monkeypatch):
    """The projection's global reads do not grow with the population it projects onto."""
    db, _default, second, _third = accounts
    _ready(db, second)
    _seed_population(db, second["id"], 6)
    db.commit()

    calls, reads = _count_reads(monkeypatch)
    _project(db, second["id"])
    counted_calls, counted = list(calls), list(reads)

    # Six threads ask the wrapped readers 51 times; the memo answers from seven real reads,
    # two of them the vendor's accounts and the model availability derived from them.
    assert len(counted_calls) == 51, counted_calls
    assert len(counted) == 7, counted
    assert len(_family(counted_calls, "effective_policy")) == 18, counted_calls
    assert len(_family(counted_calls, "project_exists")) == 12, counted_calls
    for fact in ("effective_policy", "project_exists", "get_account", "model_vendor", "account_selection_facts",
                 "list_accounts", "account_rows"):
        assert len(_family(counted, fact)) == 1, (fact, counted)
    # A thread's own facts are its own: nothing thread-specific is ever memoized.
    assert not [key for key in counted_calls if "thread" in key[1]]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_no_in_scope_consumer_mutates_a_shared_answer(vendors, monkeypatch, provider):
    """The memo shares one answer per fact, and a whole projection must not change any of them."""
    from connections import fact_memo

    db, vendor_accounts = vendors
    account = vendor_accounts[provider]
    model = "claude-code/test" if provider == "claude-code" else "codex/test"
    if provider == "claude-code":
        db.execute("INSERT OR REPLACE INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
                   "VALUES (?,'Test',?,'claude-code','test','claude-code','t','t')", (model, model))
    _seed_population(db, account["id"], 6, provider=provider, model=model)
    _ready_vendor(db, account, provider, model)

    stored = _digest_answers(monkeypatch)
    assert fact_memo.in_scope() is False
    _project(db, account["id"])

    # Every stored answer still digests to what it was when the scope stored it.
    for key, (before, answer) in stored.items():
        assert _answer_digest(answer) == before, f"{key} was changed in place"
    assert len(stored) == EXPECTED_ANSWERS[provider], sorted(key[1] for key in stored)


def test_the_same_reads_scale_with_the_population_once_the_scope_is_disabled(accounts, monkeypatch):
    """The ceiling above is load-bearing: with no scope, every one of those calls is a read."""
    db, _default, second, _third = accounts
    _ready(db, second)
    _seed_population(db, second["id"], 6)
    db.commit()

    calls, reads = _count_reads(monkeypatch)
    _without_scope(monkeypatch)
    _project(db, second["id"])

    assert len(calls) == len(reads) == 61, (calls, reads)
    assert len(_family(reads, "effective_policy")) == 18, reads
    assert len(_family(reads, "project_exists")) == 12, reads


def test_projection_writes_identical_rows_with_and_without_the_scope(accounts, tmp_path, monkeypatch):
    """Row-level parity by SHA-256 between the memo arm and the same path with no scope."""
    db, _default, second, third = accounts
    _freeze(monkeypatch)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]],
                      default_account_id=second["id"], db=db)
    _thread(db, "held"); _bind(db, "held", "codex", second["id"])
    _thread(db, "clear"); _bind(db, "clear", "codex", third["id"])
    _thread(db, "empty-project", project=""); _bind(db, "empty-project", "codex", second["id"])
    _thread(db, "gone-project", project="gone"); _bind(db, "gone-project", "codex", second["id"])
    _accept(db, "queued-brief")
    _held(db, "queued-brief")
    db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (second["id"],))
    db.commit()

    before = _digest(db)
    oracle = _copy(db, tmp_path / "no-memo.db")
    try:
        real_scope = _without_scope(monkeypatch)
        _project(db=oracle, account_id=second["id"], scope="test_account_hold_projection_no_memo")
        monkeypatch.setattr("connections.fact_memo.fact_reads", real_scope, raising=False)
        _project(db, second["id"])

        assert _rows(db, HOLD_ROWS_SQL) == _rows(oracle, HOLD_ROWS_SQL)
        assert _rows(db, QUEUED_ROWS_SQL) == _rows(oracle, QUEUED_ROWS_SQL)
        assert _digest(db) == _digest(oracle) != before
    finally:
        oracle.close()


def test_projection_never_serves_a_stale_fact_and_closes_its_scope(accounts, monkeypatch):
    """Every wrapped fact reads the same after the projection as before it, and the scope is shut."""
    from connections import fact_memo

    db, _default, second, _third = accounts
    _freeze(monkeypatch)
    _ready(db, second)
    _seed_population(db, second["id"], 4)
    db.commit()
    before = _facts(db, "destination-project")

    _project(db, second["id"])

    assert _facts(db, "destination-project") == before
    assert fact_memo.in_scope() is False
    assert fact_memo.memoized(("probe",), lambda: "fresh") == "fresh"


def test_projection_makes_no_thread_or_task_hop(accounts, monkeypatch):
    """The scope is a plain synchronous call stack: nothing can carry it out of the walk."""
    from connections import fact_memo

    db, _default, second, _third = accounts
    _ready(db, second)
    _seed_population(db, second["id"], 4)
    db.commit()

    hops: list[str] = []

    def stop(name):
        def refusal(*_args, **_kwargs):
            hops.append(name)
            raise RuntimeError(f"{name} inside the projection scope")
        return refusal

    monkeypatch.setattr(threading.Thread, "start", stop("thread"))
    monkeypatch.setattr(asyncio, "to_thread", stop("to_thread"))
    monkeypatch.setattr(contextvars, "copy_context", stop("copy_context"))

    _project(db, second["id"])
    assert hops == []
    assert fact_memo.in_scope() is False


def test_wake_true_registers_its_effect_outside_the_scope(accounts, monkeypatch):
    """The after-commit wake runs once the scope is shut, so a woken turn never sees a memoized fact."""
    from connections import fact_memo

    db, _default, second, _third = accounts
    _freeze(monkeypatch)
    _discover(db, "codex", second)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]],
                      default_account_id=second["id"], db=db)
    _accept(db, "queued-brief")
    _held(db, "queued-brief", cause="account_signed_out")
    db.commit()

    woken: list[tuple] = []
    monkeypatch.setattr("infra.state.ws.schedule_models_changed",
                        lambda: woken.append(("models", fact_memo.in_scope())))

    # The account is usable again while the thread's head is still held for it, so the
    # retained owner registers its wake; the effect runs after the commit, outside the scope.
    _project(db, second["id"], wake=True)
    assert woken == [("models", False)]

    woken.clear()
    _project(db, second["id"], wake=False)
    assert woken == []