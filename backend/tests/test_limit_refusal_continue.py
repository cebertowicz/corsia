"""A vendor usage-limit refusal of a harness Turn leaves the one continue input behind.

The Turn ends ``error`` and its account is recorded limited; the continue input
is what drives the account rollover (or the account-limit hold) with no new
person input. Pins the moves, the holds and the bounds that stop a refusal loop.
"""
from __future__ import annotations

import json

import pytest

from connections import account_moves, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt
from connections.vendor_accounts import default_account
from infra.database import get_db
from infra.plan_limit_continue import CONTINUE_CHIP, continue_message_id
from infra.thread_driver import TerminalResult, finish_turn
from infra.thread_input_types import ClaimedBatch, HeldAdmission
from infra.thread_inputs import start_idle_turn
from vendor_account_fixtures import accounts, receipt_of, vendors  # noqa: F401
from vendor_account_fixtures import _accept, set_legacy_policy

_REFUSAL = "You've hit your session limit · resets 1am (America/Los_Angeles)"
_PROVIDERS = {"codex": ("destination", "codex/test"), "claude-code": ("cdest", "claude-code/test")}


def _setup(accounts, vendors, monkeypatch, provider, *, rollover):
    """A thread bound to a provider's first account, with a usable second account."""
    db, extra = vendors
    thread, model = _PROVIDERS[provider]
    if provider == "claude-code":
        db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
                   "VALUES (?,'user',?,'destination-project','t','t')", (thread, model))
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,context_window,created_at,updated_at) "
                   "VALUES (?,'Test',?,'claude-code','test','claude-code',200000,'t','t')", (model, model))
        db.commit()
    first, second = (default_account(provider), extra[provider]) if provider == "claude-code" else accounts[2:]
    for row in (first, second):
        mark_discovery(row["id"], receipt=capture_receipt(provider, account_id=row["id"], db=db), status="ready",
                       models={model: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    set_legacy_policy("destination-project", provider, allowlist=[first["id"], second["id"]],
                      default_account_id=first["id"], switch_on_limit=rollover, db=db)
    select_account(thread, provider, db=db)
    for row in (first, second):
        harness_usage.record_allowed(provider=provider, receipt=receipt_of(provider, row["id"], db=db),
                                     source="discovery", db=db)
    events = []
    monkeypatch.setattr(account_moves, "_publish", events.append)
    return db, thread, first, second, events


def _running_turn(db, thread, turn="t1", message="m1"):
    """A Turn that already delivered its input: the work was underway when the vendor refused."""
    _accept_to(db, thread, message)
    batch = start_idle_turn(thread, turn_id=turn, boot_epoch="test", started_at="t", db=db)
    assert isinstance(batch, ClaimedBatch)
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id=?", (message,))
    db.commit()


def _accept_to(db, thread, identity):
    from infra.input_admission import accept_thread_input

    return accept_thread_input(thread, role="user", content=identity, intent="apply_now",
                               message_id=identity, db=db)


def _refuse(db, turn="t1", error=_REFUSAL):
    return finish_turn(turn, outcome="error", ended_at="t2", successor_turn_id=f"next-{turn}",
                       boot_epoch="test", error=error, db=db)


def _continues(db, thread):
    return [dict(row) for row in db.execute(
        "SELECT id,delivery_state,active_turn_id,metadata FROM messages "
        "WHERE thread_id=? AND id LIKE 'overage-continue:%' ORDER BY id", (thread,))]


def _running(db, thread):
    return db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'", (thread,)).fetchone()[0]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_refusal_with_rollover_moves_to_the_next_account_and_continues(accounts, vendors, monkeypatch, provider):
    from core.stop_causes import VENDOR_LIMIT_REFUSAL_REASON

    db, thread, first, second, events = _setup(accounts, vendors, monkeypatch, provider, rollover=True)
    _running_turn(db, thread)

    terminal = _refuse(db)

    assert isinstance(terminal.successor, ClaimedBatch)
    assert [item.message_id for item in terminal.successor.items] == [continue_message_id("t1")]
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?",
                      (terminal.successor.turn_id,)).fetchone()[0] == second["id"]
    assert len(events) == 1  # the account move receipt
    (row,) = _continues(db, thread)
    metadata = json.loads(row["metadata"])
    assert (metadata["cause"], metadata["event_type"], metadata["summary"]) == (
        VENDOR_LIMIT_REFUSAL_REASON, VENDOR_LIMIT_REFUSAL_REASON, CONTINUE_CHIP)
    assert metadata["stopped_turn_id"] == "t1"
    assert db.execute("SELECT idle_checkpoint_pending FROM active_turns WHERE id='t1'").fetchone()[0] == 0
    assert terminal.idle_checkpoint is None


def test_refusal_with_rollover_off_holds_one_continue_and_starts_no_turn(accounts, vendors, monkeypatch):
    db, thread, first, second, events = _setup(accounts, vendors, monkeypatch, "codex", rollover=False)
    _running_turn(db, thread)

    terminal = _refuse(db)

    assert isinstance(terminal.successor, HeldAdmission)
    assert _running(db, thread) == 0
    assert events == []
    (row,) = _continues(db, thread)
    assert (row["id"], row["delivery_state"], row["active_turn_id"]) == (continue_message_id("t1"), "queued", None)
    assert db.execute("SELECT idle_checkpoint_pending FROM active_turns WHERE id='t1'").fetchone()[0] == 0
    assert terminal.idle_checkpoint is None


def test_a_real_queued_input_gets_no_continue(accounts, vendors, monkeypatch):
    db, thread, first, second, _ = _setup(accounts, vendors, monkeypatch, "codex", rollover=False)
    _running_turn(db, thread)
    _accept_to(db, thread, "real")

    _refuse(db)

    assert _continues(db, thread) == []


def test_a_second_terminal_for_the_same_turn_is_a_duplicate(accounts, vendors, monkeypatch):
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=False)
    _running_turn(db, thread)
    _refuse(db)

    again = _refuse(db)

    assert again.result is TerminalResult.ALREADY_SAME_OUTCOME
    assert [row["id"] for row in _continues(db, thread)] == [continue_message_id("t1")]


def test_a_non_limit_error_gets_no_continue(accounts, vendors, monkeypatch):
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)

    _refuse(db, error="Process exited with code 1")

    assert _continues(db, thread) == []


def test_an_unrecorded_limit_fails_closed_without_a_continue(accounts, vendors, monkeypatch):
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)

    def broken(*args, **kwargs):
        raise RuntimeError("verdict write refused")

    monkeypatch.setattr("infra.terminal_usage_limits._record", broken)

    terminal = _refuse(db)

    assert _continues(db, thread) == []
    assert _running(db, thread) == 0
    assert "usage-limit verdict not recorded" in (
        db.execute("SELECT error FROM active_turns WHERE id='t1'").fetchone()[0])
    assert terminal.successor is None or not isinstance(terminal.successor, ClaimedBatch)


def test_a_refusal_with_no_login_receipt_gets_no_continue(accounts, vendors, monkeypatch):
    # No receipt means no account verdict was written, so a continue would be admitted
    # straight back onto the refusing account and repeat without bound.
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)
    db.execute("UPDATE active_turns SET vendor_account_id=NULL WHERE id='t1'")
    db.commit()

    terminal = _refuse(db)

    assert _continues(db, thread) == []
    assert not isinstance(terminal.successor, ClaimedBatch)
    assert _running(db, thread) == 0


def test_a_refusal_on_a_model_with_no_harness_account_gets_no_continue(accounts, vendors, monkeypatch):
    # The Turn has a login receipt but its model names no harness connection, so no verdict is written.
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)
    db.execute("UPDATE active_turns SET model='external@none/unknown' WHERE id='t1'")
    db.commit()

    terminal = _refuse(db)

    assert _continues(db, thread) == []
    assert not isinstance(terminal.successor, ClaimedBatch)


def test_a_stop_whose_error_reads_as_a_limit_does_not_continue(accounts, vendors, monkeypatch):
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)

    finish_turn("t1", outcome="cancelled", ended_at="t2", successor_turn_id="next-t1",
                boot_epoch="test", error=_REFUSAL, db=db)

    assert _continues(db, thread) == []
    assert _running(db, thread) == 0


def test_a_completed_turn_whose_error_reads_as_a_limit_does_not_continue(accounts, vendors, monkeypatch):
    # A finished Turn is never re-run: the model would be told to continue work it completed.
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)

    finish_turn("t1", outcome="completed", ended_at="t2", successor_turn_id="next-t1",
                boot_epoch="test", error=_REFUSAL, db=db)

    assert _continues(db, thread) == []


def test_a_delegated_child_gets_the_continue_too(accounts, vendors, monkeypatch):
    db, thread, *_ = _setup(accounts, vendors, monkeypatch, "codex", rollover=False)
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
               "VALUES ('parent','user','codex/test','t','t')")
    db.execute("UPDATE threads SET spawned_by='parent' WHERE id=?", (thread,))
    db.commit()
    _running_turn(db, thread)

    _refuse(db)

    assert [row["id"] for row in _continues(db, thread)] == [continue_message_id("t1")]


def test_a_continue_refused_again_on_the_same_login_stops_instead_of_cycling(accounts, vendors, monkeypatch):
    db, thread, first, second, _ = _setup(accounts, vendors, monkeypatch, "codex", rollover=False)
    _running_turn(db, thread)
    _refuse(db)
    # Admission judges the account usable again (reset passed or verdict re-observed) and claims the continue.
    harness_usage.record_allowed(provider="codex", receipt=receipt_of("codex", first["id"], db=db),
                                 source="discovery", db=db)
    batch = start_idle_turn(thread, turn_id="t2", boot_epoch="test", started_at="t", db=db)
    assert isinstance(batch, ClaimedBatch) and [item.message_id for item in batch.items] == [continue_message_id("t1")]
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id=?", (continue_message_id("t1"),))
    db.commit()

    _refuse(db, turn="t2")

    assert [row["id"] for row in _continues(db, thread)] == [continue_message_id("t1")]


def test_a_continue_refused_on_the_next_account_walks_on(accounts, vendors, monkeypatch):
    db, thread, first, second, _ = _setup(accounts, vendors, monkeypatch, "codex", rollover=True)
    _running_turn(db, thread)
    moved = _refuse(db)
    assert isinstance(moved.successor, ClaimedBatch)
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id=?", (continue_message_id("t1"),))
    db.commit()

    terminal = _refuse(db, turn=moved.successor.turn_id)

    assert {row["id"] for row in _continues(db, thread)} == {
        continue_message_id("t1"), continue_message_id(moved.successor.turn_id)}
    assert isinstance(terminal.successor, HeldAdmission)  # both accounts limited: held, no cycle
    assert _running(db, thread) == 0
