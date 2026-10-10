"""A binding change retires the provider session its old account created.

A Codex or Claude Code session lives under its account's home, so the next turn
after a move must start fresh. An unchanged binding keeps its session, so a
restart or eviction still resumes it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from connections.account_selection import rebind_idle_account
from infra.database import write_transaction
from registry.listing import find_for_thread
from registry.service import register_cc_session, register_codex_thread
from vendor_account_fixtures import THREAD, _accept, _configured, _move, accounts, claude  # noqa: F401


def _epoch(db, thread_id):
    return db.execute("SELECT runtime_context_epoch FROM threads WHERE id=?", (thread_id,)).fetchone()[0]


def _codex(db):
    return find_for_thread("destination", "codex", "thread", db=db)


def test_codex_rollover_retires_the_old_account_session_before_the_next_turn(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch)
    register_codex_thread("destination", "old-account-rollout", db=db)
    db.commit()
    _accept(db, "input")
    with write_transaction(db, scope="committed_move"):
        assert _move(db) is not None
    assert _codex(db) is None


def test_codex_rebind_retires_the_session_and_an_unchanged_binding_keeps_it(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch)
    register_codex_thread("destination", "old-account-rollout", db=db)
    db.commit()
    same = rebind_idle_account("destination", first["id"], expected_runtime_epoch=_epoch(db, "destination"),
                               user_id="user", db=db)
    assert same["changed"] is False
    assert _codex(db)["external_id"] == "old-account-rollout"
    moved = rebind_idle_account("destination", second["id"], expected_runtime_epoch=_epoch(db, "destination"),
                                user_id="user", db=db)
    assert moved["changed"] is True
    assert _codex(db) is None
    switches = db.execute("SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchall()
    assert len(switches) == 1 and 'account_pinned' in switches[0]['metadata']
    retried = rebind_idle_account("destination", second["id"], expected_runtime_epoch=_epoch(db, "destination"),
                                  user_id="user", db=db)
    assert retried["changed"] is False
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_pinned_move_keeps_context_boundary_effects(monkeypatch):
    from connections.account_moves import _post_effects

    boundary, receipt = [], []

    async def record_boundary(thread_id, *, user_id, reason):
        boundary.append((thread_id, user_id, reason))

    async def record_receipt(value):
        receipt.append(value)

    monkeypatch.setattr('thread.runtime_boundary.post_context_boundary_effects', record_boundary)
    monkeypatch.setattr('thread.model_runtime.post_account_switch_effects', record_receipt)
    value = {'thread_id': 'thread', 'user_id': 'user'}
    await _post_effects(value)
    assert boundary == [('thread', 'user', 'vendor_account_changed')]
    assert receipt == [value]


def test_claude_rebind_retires_the_session_and_an_unchanged_binding_keeps_it(claude):
    db = claude.db
    db.execute("DELETE FROM active_turns WHERE id='claude-turn'")
    db.commit()
    register_cc_session(THREAD, "old-account-session", db=db)
    db.commit()
    same = rebind_idle_account(THREAD, claude.first, expected_runtime_epoch=0, user_id="user", db=db)
    assert same["changed"] is False
    assert find_for_thread(THREAD, "claude-code", "session", db=db)["external_id"] == "old-account-session"
    moved = rebind_idle_account(THREAD, claude.other, expected_runtime_epoch=0, user_id="user", db=db)
    assert moved["changed"] is True
    assert find_for_thread(THREAD, "claude-code", "session", db=db) is None


def test_a_claude_account_chosen_on_a_codex_thread_applies_at_its_first_claude_claim(accounts):
    from connections.account_moves import apply_pending_accounts
    from connections.account_policy import select_account, turn_account
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state

    db, legacy, _, _ = accounts
    select_account("destination", "codex", db=db)
    register_codex_thread("destination", "live-codex-rollout", db=db)
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/synthetic"})
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('claude-code/test','Test','claude-code/test','claude-code','test','claude-code','t','t')")
    db.commit()
    first = set_account_state(add_account("claude-code", "First")["id"], "signed_in", expected_revision=0)
    second = set_account_state(add_account("claude-code", "Second")["id"], "signed_in", expected_revision=0)
    db.commit()
    epoch = _epoch(db, "destination")

    def binding(provider):
        row = db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                         "WHERE thread_id='destination' AND provider=?", (provider,)).fetchone()
        return row and tuple(row)

    def switches():
        return db.execute("SELECT COUNT(*) FROM messages "
                          "WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0]

    def codex_untouched():
        assert binding("codex") == codex and _codex(db)["external_id"] == "live-codex-rollout"
        assert _epoch(db, "destination") == epoch and switches() == 0

    codex = binding("codex")
    # No Claude binding yet: the creation owner binds the chosen account.
    created = rebind_idle_account("destination", first["id"], user_id="user", db=db)
    assert (created["changed"], created["pending"]) == (True, True) and binding("claude-code") == (first["id"], None)
    codex_untouched()
    register_cc_session("destination", "first-account-session", db=db)
    db.commit()

    # Another Claude account is held for the thread's next Claude turn, which runs on it.
    held = rebind_idle_account("destination", second["id"], user_id="user", db=db)
    assert held["pending"] is True and binding("claude-code") == (first["id"], second["id"])
    assert turn_account("claude-code", thread_id="destination", db=db)[0] == second["id"]
    codex_untouched()
    assert find_for_thread("destination", "claude-code", "session", db=db) is not None

    # A running Codex turn does not change that, and a Codex claim leaves the Claude choice held.
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES ('codex-turn','destination',0,'codex/test','running','t','boot')")
    db.commit()
    assert rebind_idle_account("destination", second["id"], user_id="user", db=db)["pending"] is True
    db.execute("DELETE FROM active_turns WHERE id='codex-turn'")
    db.commit()
    with write_transaction(db, scope="codex_claim"):
        apply_pending_accounts("destination", db=db)
    assert binding("claude-code") == (first["id"], second["id"])
    codex_untouched()

    # The first Claude claim moves to it and retires the old account's Claude session.
    db.execute("UPDATE threads SET model='claude-code/test' WHERE id='destination'")
    db.commit()
    with write_transaction(db, scope="claude_claim"):
        apply_pending_accounts("destination", db=db)
    assert binding("claude-code") == (second["id"], None) and switches() == 1
    assert find_for_thread("destination", "claude-code", "session", db=db) is None
    assert binding("codex") == codex


def test_a_held_choice_is_not_rolled_over_when_the_thread_binds_its_model(accounts, monkeypatch):
    from connections.account_selection import bind_model_account

    db, first, second, events = _configured(accounts, monkeypatch)
    legacy = accounts[1]
    db.execute("UPDATE thread_vendor_accounts SET pending_account_id=? WHERE thread_id='destination' AND provider='codex'",
               (legacy["id"],))
    db.commit()
    with write_transaction(db, scope="bind_switched_model"):
        bind_model_account("destination", "codex/test", db=db)
    assert tuple(db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                            "WHERE thread_id='destination' AND provider='codex'").fetchone()) == (first["id"], legacy["id"])
