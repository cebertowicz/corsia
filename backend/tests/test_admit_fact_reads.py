"""Admission reuses only facts its own writes cannot change, within one call."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from datetime import datetime

import pytest

from vendor_account_fixtures import accounts, vendors  # noqa: F401

NOW = "2026-11-14T12:00:00+00:00"
THREAD = "memo-admission"


def _freeze(monkeypatch):
    for module in ("infra.time", "infra.input_admission", "infra.passive_changes",
                   "connections.account_moves", "connections.account_holds"):
        monkeypatch.setattr(f"{module}.utc_now_iso", lambda: NOW)
    monkeypatch.setattr("connections.harness_usage._now", lambda: datetime.fromisoformat(NOW))


def _seed(db, provider, destination, shape):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import list_accounts
    from infra.input_admission import accept_thread_input

    model = f"{provider}/test"
    source = next(a for a in list_accounts(provider=provider, db=db) if a["is_default"])
    for account in (source, destination):
        receipt = capture_receipt(provider, account_id=account["id"], db=db)
        assert receipt is not None
        assert mark_discovery(account["id"], receipt=receipt, status="ready",
                              models={model: {"available": True}}, db=db)
    db.commit()
    if shape == "rollover":
        from connections.harness_usage import record_allowed

        record_allowed(provider=provider, source="discovery",
                       receipt=capture_receipt(provider, account_id=destination["id"], db=db), db=db)
    db.execute("INSERT INTO threads(id,user_id,project_id,model,created_at,updated_at) "
               "VALUES (?,'user','destination-project',?,'t','t')", (THREAD, model))
    if shape != "unbound":
        pending = destination["id"] if shape == "pending" else None
        db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id,pending_account_id) "
                   "VALUES (?,?,?,?)", (THREAD, provider, source["id"], pending))
    if shape in {"denied", "rollover"}:
        db.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES ('destination-project',1)")
        db.execute("INSERT INTO provider_policies(project_id,provider_key,rollover,order_json,disabled_json,revision) "
                   "VALUES ('destination-project',?,?,?,?,1)",
                   (provider, int(shape == "rollover"), json.dumps([destination["id"]]), json.dumps([source["id"]])))
    if shape == "signed_out":
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (source["id"],))
    if shape == "running":
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
                   "VALUES ('prior',?,0,?,'running','t','test')", (THREAD, model))
    db.execute("INSERT INTO context_change_journal(object_kind,object_id,change_kind,summary,details_json,created_at) "
               "VALUES ('thread_context',?,'changed','A passive note','{}',?)", (THREAD, NOW))
    db.commit()
    if shape == "duplicate":
        accept_thread_input(THREAD, role="user", content="hello", intent="apply_now",
                            message_id="input", created_at=NOW, db=db)
    db.commit()
    return source, model


def _admit(db=None, *, message="input", turn="turn"):
    from infra.thread_driver import admit_input

    return admit_input(THREAD, role="user", content="hello", intent="apply_now",
                       message_id=message, turn_id=turn, boot_epoch="test", admitted_at=NOW, db=db)


def _snapshot(db):
    """All persisted rows, including digest/input text, Turn and account bindings."""
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr)
            for name in tables}


def _audit(monkeypatch):
    """Check at every cache hit and at scope exit, including shared object identity."""
    from connections import fact_memo

    real_memoized = fact_memo.memoized
    entries = {}
    hits = []

    def serial(answer):
        return json.dumps(answer, sort_keys=True, default=str)

    def fresh(read):
        token = fact_memo._answers.set(None)
        try:
            return read()
        finally:
            fact_memo._answers.reset(token)

    def memoized(key, read):
        scope = fact_memo._answers.get()
        if scope is None:
            return real_memoized(key, read)
        # Retain the scope object too: nested decorator scopes can otherwise reuse ids.
        _scope, saved = entries.setdefault(id(scope), (scope, {}))
        if key in scope:
            digest, answer, _read = saved[key]
            assert serial(answer) == digest, ("mutated at hit", key)
            assert serial(fresh(read)) == digest, ("stale at hit", key)
            hits.append(key)
        answer = real_memoized(key, read)
        saved.setdefault(key, (serial(answer), answer, read))
        return answer

    def verify():
        checked = []
        for _scope, saved in list(entries.values()):
            for key, (digest, answer, read) in saved.items():
                assert serial(answer) == digest, ("mutated at exit", key)
                assert serial(fresh(read)) == digest, ("stale at exit", key)
            checked.extend(saved)
        return checked

    monkeypatch.setattr(fact_memo, "memoized", memoized)
    return verify, hits


@pytest.mark.parametrize("provider", ["codex", "claude-code"])
@pytest.mark.parametrize("shape", ["unbound", "bound", "pending", "denied", "rollover", "signed_out", "running", "duplicate"])
def test_admission_memo_preserves_every_row_and_answer(vendors, monkeypatch, provider, shape):
    from connections import fact_memo
    from infra.database import write_transaction
    from infra.thread_driver import admit_input

    real_scope = fact_memo.fact_reads

    def without_admit_scope():
        # Retain all pre-existing nested scopes; disable only the new admit scope.
        return contextlib.nullcontext() if sys._getframe(1).f_code is admit_input.__code__ else real_scope()

    db, accounts_by_provider = vendors
    _freeze(monkeypatch)
    source, _model = _seed(db, provider, accounts_by_provider[provider], shape)
    # In-memory SQLite backups keep each arm byte-for-byte at the same starting state.
    baseline, candidate = (sqlite3.connect(":memory:") for _ in range(2))
    for arm in (baseline, candidate):
        arm.row_factory = sqlite3.Row
        db.backup(arm)
    try:
        with monkeypatch.context() as plain:
            plain.setattr(fact_memo, "fact_reads", without_admit_scope)
            with write_transaction(baseline, scope="parity_owner"):
                expected = _admit(baseline)
                expected_rows = _snapshot(baseline)
        verify, hits = _audit(monkeypatch)
        with write_transaction(candidate, scope="parity_owner"):
            actual = _admit(candidate)
            checked = verify()
            assert not fact_memo.in_scope()
            assert actual == expected
            assert _snapshot(candidate) == expected_rows
        assert actual.passive is not None
        if shape in {"unbound", "bound", "pending", "rollover"}:
            assert actual.batch is not None
            assert checked and hits
            assert any(key[:2] == ("connections.account_home", "missing_home_reason") for key in checked)
            bound = candidate.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                                      (THREAD, provider)).fetchone()[0]
            assert bound == (accounts_by_provider[provider]["id"] if shape in {"pending", "rollover"} else source["id"])
        else:
            assert actual.batch is None
    finally:
        baseline.close()
        candidate.close()


def test_memo_closes_before_commit_effects_and_next_admission(vendors, monkeypatch):
    from connections import fact_memo
    from infra.write_effects import after_commit
    import infra.thread_driver as driver

    db, accounts_by_provider = vendors
    _freeze(monkeypatch)
    source, _model = _seed(db, "codex", accounts_by_provider["codex"], "bound")
    real_start = driver.start_idle_turn
    observed = []

    def start(*args, **kwargs):
        after_commit(kwargs["db"], lambda: observed.append(fact_memo.in_scope()))
        return real_start(*args, **kwargs)

    monkeypatch.setattr(driver, "start_idle_turn", start)
    assert _admit().batch is not None  # production caller owns its connection
    assert observed == [False]
    assert not fact_memo.in_scope()
    db.execute("UPDATE active_turns SET status='completed' WHERE thread_id=?", (THREAD,))
    db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (source["id"],))
    db.commit()
    assert _admit(message="second", turn="second-turn").batch is None
    assert not fact_memo.in_scope()


def test_failed_admission_closes_scope_and_rolls_back(vendors, monkeypatch):
    from connections import fact_memo
    import infra.thread_driver as driver

    db, accounts_by_provider = vendors
    _freeze(monkeypatch)
    _seed(db, "codex", accounts_by_provider["codex"], "bound")
    before = _snapshot(db)

    def fail(*args, **kwargs):
        assert fact_memo.in_scope()
        raise RuntimeError("claim failed")

    monkeypatch.setattr(driver, "start_idle_turn", fail)
    with pytest.raises(RuntimeError, match="claim failed"):
        _admit(db)
    assert not fact_memo.in_scope()
    assert _snapshot(db) == before
