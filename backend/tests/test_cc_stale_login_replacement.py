"""A live Claude Code process admitted on a login that is no longer current is replaced.

Its frozen receipt can never pass the paid-write check again, so reusing it refuses
every later Turn with ``account_login_unverified`` while the account itself is
signed in. The feed must replace it, like a switched or stale-cwd process, so the
next launch freezes the Turn's own current login.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connections.account_receipts import capture_receipt
from connections.vendor_accounts import get_account, set_account_state
from subagents import cc_feed_transition
from vendor_account_fixtures import signed_in_default

from cc_runtime_fixtures import (  # noqa: F401
    _seed_parent,
    _wait,
    child_db,
    cc_target,
    fake_claude,
    feed_with_persisted_input,
)

_TURN_ID = "22222222-3333-4444-8555-666666666666"


def _live(receipt, *, returncode=None):
    session = SimpleNamespace(
        returncode=returncode, working_dir=None,
        vendor_account_bound=receipt is not None, vendor_account_receipt=receipt,
    )
    return SimpleNamespace(
        session=session, resume_rejected=False, model="claude-code/sonnet",
        expected_reported_model=None, provider_context_untrusted=False,
    )


def _transition(db):
    return SimpleNamespace(
        thread_id="destination", db=db,
        target=SimpleNamespace(transport_model="claude-code/sonnet", expected_reported_model=None),
    )


@pytest.fixture
def signed_in(isolated_db):
    from infra.database import get_db

    db = get_db()
    account = signed_in_default("claude-code", db=db)
    yield db, account, capture_receipt("claude-code", account_id=account["id"], db=db)
    db.close()


def _sign_in_again(db, account):
    current = get_account(account["id"], db=db)
    set_account_state(current["id"], "signed_in", expected_revision=current["revision"], db=db)


def test_process_on_a_replaced_login_needs_replacement(signed_in):
    db, account, receipt = signed_in
    _sign_in_again(db, account)

    assert cc_feed_transition._replacement_reason(_transition(db), _live(receipt))[0] == "stale_login"


def test_process_on_a_signed_out_login_needs_replacement(signed_in):
    db, account, receipt = signed_in
    current = get_account(account["id"], db=db)
    set_account_state(current["id"], "signed_out", expected_revision=current["revision"], db=db)

    assert cc_feed_transition._replacement_reason(_transition(db), _live(receipt))[0] == "stale_login"


def test_process_on_the_current_login_is_kept(signed_in):
    db, _account, receipt = signed_in

    assert cc_feed_transition._replacement_reason(_transition(db), _live(receipt)) == (None, None)


def test_unbound_process_is_not_judged_by_login(signed_in):
    db, _account, _receipt = signed_in

    assert cc_feed_transition._replacement_reason(_transition(db), _live(None)) == (None, None)


def test_dead_process_keeps_its_dead_replacement(signed_in):
    db, account, receipt = signed_in
    _sign_in_again(db, account)

    assert cc_feed_transition._replacement_reason(_transition(db), _live(receipt, returncode=1))[0] == "dead"


@pytest.mark.asyncio
async def test_next_turn_after_a_new_login_runs_on_a_replacement_process(
    child_db, fake_claude, monkeypatch, tmp_path,
):
    import connections.models as cm
    import subagents.cc_runtime as rt
    from connections.vendor_accounts import resolve_account_id
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr(cm, "get_connection", lambda provider, db=None: {"config": {"binary": fake_claude}})
    _seed_parent(child_db)
    child_db.execute("UPDATE threads SET model='claude-code/sonnet' WHERE id='parent'")
    child_db.commit()
    assert admit_input(
        "parent", role="user", content="spawn a child", intent="apply_now",
        message_id="parent:active-input", turn_id="parent:active-turn",
        boot_epoch=BOOT_EPOCH, admitted_at="t", db=child_db,
    ).batch is not None
    child_id = await rt.spawn(
        "parent", "do it", binary=fake_claude, target=cc_target("sonnet"),
        broadcast=lambda _sid, _message: None,
    )
    assert await _wait(lambda: rt.is_live(child_id))
    assert await _wait(lambda: child_db.execute(
        "SELECT status FROM active_turns WHERE thread_id=? ORDER BY turn_order DESC LIMIT 1",
        (child_id,),
    ).fetchone()["status"] == "completed")
    assert await _wait(lambda: not rt._LIVE.get(child_id).turn_active)
    first = rt._LIVE.get(child_id).session
    old_receipt = first.vendor_account_receipt
    assert old_receipt is not None

    # A sign-out and a new sign-in on the same account: its login generation moves on.
    account = get_account(resolve_account_id("claude-code", db=child_db), db=child_db)
    set_account_state(account["id"], "signed_in", expected_revision=account["revision"], db=child_db)
    signed_in_default("claude-code", db=child_db)
    child_db.commit()

    did = await feed_with_persisted_input(child_id, "again", target=cc_target("sonnet"), turn_id=_TURN_ID)

    assert did is not None and rt.is_live(child_id)
    replacement = rt._LIVE.get(child_id).session
    assert replacement is not first
    assert replacement.vendor_account_receipt != old_receipt
    assert await _wait(lambda: any(
        "echo:" in (row["content"] or "") and "again" in (row["content"] or "")
        for row in child_db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='assistant'", (child_id,)).fetchall()))
    await rt.retire(child_id)


@pytest.mark.asyncio
async def test_stale_login_closes_the_live_process_then_resumes(signed_in, monkeypatch):
    db, account, receipt = signed_in
    _sign_in_again(db, account)
    live, replacement = _live(receipt), object()
    closed = AsyncMock(return_value=True)
    monkeypatch.setattr(cc_feed_transition, "close_and_finalize", closed)
    monkeypatch.setattr(cc_feed_transition, "finalize", lambda *_args: pytest.fail("a live process is closed, not finalized"))
    transition = _transition(db)
    transition.bcast = lambda *_args: None
    transition.resume = AsyncMock(return_value=replacement)

    assert await cc_feed_transition.replace_if_required(transition, live) is replacement
    closed.assert_awaited_once_with("destination", live, transition.bcast, db)
    transition.resume.assert_awaited_once_with()
