"""The discovery commit holds the account writer for as little CPU as it can, and writes the same rows.

``commit_discovery_bundle`` projects one account's discovery onto every thread it can run,
inside the one ``account_discovery_bundle`` writer. Its cost is the per-thread reads, so two
readers stop paying for what they do not use: a reader of one answer takes
``discovery_view`` (top level copied, nothing deep-copied) instead of ``discovery_record``,
and ``fact_memo`` keys a call without ``Signature.bind``. Pins, with literal expected rows:
the view equals the record, the record stays a private deep copy, a memo key is the same
however the call spells its arguments and a bad call raises as before, and a commit leaves
the same hold rows and discovery record.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts  # noqa: F401

FROZEN_NOW = "2026-10-09T00:00:00+00:00"
MODELS = {"codex/test": {"available": True, "resolved_identity": "synthetic", "aliases": ["a", "b"]}}
FULL = {"version": 1, "login_seq": 2, "login_revision": 5, "auth_state": "known",
        "discovery": {"status": "ready", "cause": None, "observed_at": "t", "harness_version": "1",
                      "receipt": {"account_revision": 5, "login_seq": 2}, "cache_receipt": None, "models": MODELS},
        "auth_projection": {"failed": None, "stale": [], "homes": {}}}


@pytest.mark.parametrize("raw", [None, {}, {"login_seq": 7}, {"discovery": {"status": "failed", "models": {}}}, FULL])
def test_the_view_reads_exactly_like_the_record(raw):
    from connections.account_evidence import discovery_record, discovery_view

    account = {"revision": 9, "discovery": json.loads(json.dumps(raw)) if raw is not None else None}
    assert discovery_view(account) == discovery_record(account)


def test_the_view_shares_the_account_models_and_the_record_never_does():
    from connections.account_evidence import discovery_record, discovery_view

    account = {"revision": 5, "discovery": json.loads(json.dumps(FULL))}
    stored = account["discovery"]["discovery"]["models"]
    assert discovery_view(account)["discovery"]["models"] is stored
    record = discovery_record(account)
    assert record["discovery"]["models"] is not stored
    record["discovery"]["models"]["codex/test"]["available"] = False
    assert account["discovery"]["discovery"]["models"]["codex/test"]["available"] is True


def test_a_view_default_is_never_shared_between_calls():
    from connections.account_evidence import discovery_view

    first = discovery_view({"revision": 1, "discovery": None})
    first["discovery"]["models"]["x"] = {}
    assert discovery_view({"revision": 1, "discovery": None})["discovery"]["models"] == {}


def test_a_memo_key_is_the_same_however_the_call_spells_its_arguments(accounts):
    from connections import fact_memo

    db = accounts[0]
    reads = []

    def reader(identity, flag=False, *, mode="a", db):
        reads.append((identity, flag, mode))
        return len(reads)

    wrapped = fact_memo.memoized_reader(reader)
    with fact_memo.fact_reads():
        answers = {wrapped(1, db=db), wrapped(1, False, db=db), wrapped(identity=1, db=db),
                   wrapped(1, flag=False, mode="a", db=db), wrapped(1, False, mode="a", db=db)}
        assert answers == {1}
        assert wrapped(1, True, db=db) == 2
        assert wrapped(1, mode="b", db=db) == 3
        assert wrapped([1, 2], db=db) == 4
        assert wrapped((1, 2), db=db) == 4
    assert reads == [(1, False, "a"), (1, True, "a"), (1, False, "b"), ([1, 2], False, "a")]


@pytest.mark.parametrize("call", [
    lambda wrapped, db: wrapped(db=db),
    lambda wrapped, db: wrapped(1, 2, 3, db=db),
    lambda wrapped, db: wrapped(1, identity=1, db=db),
    lambda wrapped, db: wrapped(1, unknown=1, db=db),
    lambda wrapped, db: wrapped("one", db),
])
def test_a_call_the_signature_refuses_raises_inside_a_scope_and_outside(accounts, call):
    from connections import fact_memo

    db = accounts[0]

    def reader(identity, flag=False, *, db):
        return identity

    wrapped = fact_memo.memoized_reader(reader)
    with pytest.raises(TypeError):
        call(wrapped, db)
    with fact_memo.fact_reads(), pytest.raises(TypeError):
        call(wrapped, db)


def test_a_reader_with_variadic_arguments_keeps_the_bound_key(accounts):
    from connections import fact_memo

    db = accounts[0]
    reads = []

    def reader(*parts, db):
        reads.append(parts)
        return len(reads)

    wrapped = fact_memo.memoized_reader(reader)
    with fact_memo.fact_reads():
        assert wrapped(1, 2, db=db) == wrapped(1, 2, db=db) == 1
        assert wrapped(1, db=db) == 2
    assert reads == [(1, 2), (1,)]


def _bind_destination(db, account_id):
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('destination','codex',?)",
               (account_id,))
    db.execute("INSERT INTO thread_vendor_account_holds(thread_id,provider,account_id,reason,updated_at) "
               "VALUES ('destination','codex',?,'account_model_not_discovered','before')", (account_id,))
    db.commit()


def _commit(db, account, models):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt("codex", account_id=account["id"], db=db)
    result = commit_discovery_bundle(
        "codex", SimpleNamespace(usage_report=None, cache_stage=None),
        [{"selector_id": name.split("/", 1)[1], "available": True} for name in models],
        context=SimpleNamespace(receipt=receipt, account_id=account["id"]), harness_version="1", db=db)
    return result


def _state(db, account):
    holds = [tuple(row) for row in db.execute(
        "SELECT thread_id,provider,account_id,reason,updated_at FROM thread_vendor_account_holds ORDER BY thread_id")]
    record = json.loads(db.execute("SELECT discovery_json FROM vendor_accounts WHERE id=?", (account["id"],)).fetchone()[0])
    discovery = record["discovery"]
    return holds, discovery["status"], sorted(discovery["models"]), discovery["harness_version"]


def test_a_commit_that_discovers_the_threads_model_releases_its_hold(accounts, monkeypatch):
    db, _default, second, _third = accounts
    monkeypatch.setattr("connections.account_holds.utc_now_iso", lambda: FROZEN_NOW)
    _bind_destination(db, second["id"])

    result = _commit(db, second, ["codex/test"])

    assert (result.status, result.model_count) == ("ready", 1)
    assert _state(db, second) == ([], "ready", ["codex/test"], "1")


def test_a_commit_that_lacks_the_threads_model_keeps_the_hold_and_writes_the_same_rows(accounts, monkeypatch):
    db, _default, second, _third = accounts
    monkeypatch.setattr("connections.account_holds.utc_now_iso", lambda: FROZEN_NOW)
    _bind_destination(db, second["id"])

    result = _commit(db, second, ["codex/other"])

    assert (result.status, result.model_count) == ("ready", 1)
    assert _state(db, second) == (
        [("destination", "codex", second["id"], "account_model_not_discovered", "before")],
        "ready", ["codex/other"], "1")


def _thread(db, thread_id, account_id, *, model="codex/test", hold=None, queued=False):
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES (?,'user',?,'destination-project','t','t')", (thread_id, model))
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES (?,'codex',?)",
               (thread_id, account_id))
    if hold:
        db.execute("INSERT INTO thread_vendor_account_holds(thread_id,provider,account_id,reason,updated_at) "
                   "VALUES (?,'codex',?,?,'before')", (thread_id, account_id, hold))
    if queued:
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq,delivery_state,input_order,"
                   "target_model,delivery_error,write_outcome) VALUES (?,?,'user','x','t',1,'queued',1,?,?,?)",
                   (f"{thread_id}-input", thread_id, model, hold, "proved_no_write" if hold else None))


def _hold_rows(db):
    return [tuple(row) for row in db.execute(
        "SELECT thread_id,account_id,reason,updated_at FROM thread_vendor_account_holds ORDER BY thread_id")]


def test_a_multi_account_commit_writes_only_the_holds_that_change(accounts, monkeypatch):
    """Some threads change, most do not, one has a queued input, one is on another account."""
    db, _default, second, third = accounts
    monkeypatch.setattr("connections.account_holds.utc_now_iso", lambda: FROZEN_NOW)
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('codex/other','Other','codex/other','codex','other','codex','t','t')")
    missing = "account_model_not_discovered"
    _thread(db, "a-clear", second["id"])
    _thread(db, "b-released", second["id"], hold=missing)
    _thread(db, "c-kept", second["id"], model="codex/other", hold=missing)
    _thread(db, "d-new", second["id"], model="codex/other")
    _thread(db, "e-queued-released", second["id"], hold=missing, queued=True)
    _thread(db, "f-queued-kept", second["id"], model="codex/other", hold=missing, queued=True)
    _thread(db, "g-elsewhere", third["id"], model="codex/other", hold=missing)
    db.commit()

    result = _commit(db, second, ["codex/test"])

    assert (result.status, result.model_count) == ("ready", 1)
    assert _hold_rows(db) == [
        ("c-kept", second["id"], missing, "before"),
        ("d-new", second["id"], missing, FROZEN_NOW),
        ("f-queued-kept", second["id"], missing, "before"),
        ("g-elsewhere", third["id"], missing, "before"),
    ]
    assert [tuple(row) for row in db.execute(
        "SELECT id,delivery_error,write_outcome FROM messages ORDER BY id")] == [
        ("e-queued-released-input", missing, "proved_no_write"),
        ("f-queued-kept-input", missing, "proved_no_write"),
    ]


def test_the_statements_of_a_commit_do_not_grow_with_the_threads_it_leaves_unchanged(accounts, monkeypatch):
    """The cost of a discovery that changes nothing is flat in the bound threads."""
    db, _default, second, _third = accounts
    monkeypatch.setattr("connections.account_holds.utc_now_iso", lambda: FROZEN_NOW)
    seen = []
    for count, first in ((10, 0), (60, 10)):
        for index in range(first, count):
            _thread(db, f"bulk-{index}", second["id"])
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        try:
            _commit(db, second, ["codex/test"])
        finally:
            db.set_trace_callback(None)
        seen.append(len(statements))
    assert _hold_rows(db) == []
    assert seen[1] - seen[0] <= 3
