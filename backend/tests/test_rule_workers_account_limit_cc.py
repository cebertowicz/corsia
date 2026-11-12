"""A rule-started worker on a Claude Code owner whose account is usage-limited, through the real start path.

A Claude Code child's first turn is delivered by ``cc_spawn`` rather than ``send_thread_input``, so a hold
on it needs its own branch. The catalog rows, account selection, rollover and held-input gate are the real
ones; only the vendor's usage verdicts are recorded (as its limit events would) and the ``claude`` binary is
a fake that records the account root each launch ran with.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, make_rule, request, starts, world, worker_events,
)
from tests.test_rule_workers_account_limit import LIMIT, _spawned_models, _status, _workers

PROVIDER, MODEL = "claude-code", "claude-code/sonnet"


def _accounts(world, monkeypatch, *, rollover: bool, sibling: str):
    """The Default (limited) and one added account in the owner's project.

    ``sibling`` is ``unread`` (discovered, usage never read), ``allowed`` or ``limited``.
    """
    from connections import harness_usage
    from connections.account_evidence import mark_discovery
    from connections.account_policy import set_policy
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, default_account, set_account_state
    from infra.database import get_db
    from tests.vendor_account_fixtures import default_receipt

    db = get_db()
    default = default_account(PROVIDER, db=db)
    added = set_account_state(add_account(PROVIDER, "Added")["id"], "signed_in", expected_revision=0)
    receipt = capture_receipt(PROVIDER, account_id=added["id"], db=db)
    assert mark_discovery(added["id"], receipt=receipt, status="ready",
                          models={MODEL: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    if sibling == "allowed":
        harness_usage.record_allowed(provider=PROVIDER, source="discovery", db=db, receipt=receipt)
    elif sibling == "limited":
        harness_usage.record_limited(provider=PROVIDER, db=db, receipt=receipt, **LIMIT)
    harness_usage.record_limited(provider=PROVIDER, receipt=default_receipt(PROVIDER, db=db), db=db, **LIMIT)
    set_policy(world["project"], PROVIDER, rollover=rollover, order=[default["id"], added["id"]],
               disabled=[], expected_revision=0, db=db)
    monkeypatch.setattr("connections.harness_usage_check.schedule_account_check", lambda provider, account_id: None)
    from connections import account_moves

    monkeypatch.setattr(account_moves, "_publish", lambda _event: None)
    return default, added, receipt


def _turns(world) -> list[str]:
    """The account root the fake ``claude`` held for each turn it was given, in order."""
    path = Path(str(world["reply"]) + ".turns")
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.asyncio
async def test_a_held_first_turn_is_not_a_crash_and_runs_once_on_the_released_account(world, monkeypatch):
    """Rollover on, the sibling's usage unread: the spawn is held (it used to raise AttributeError on the
    held admission), the hold shows in delegate_status, nothing launches and no second start fires; once
    the sibling's usage reads allowed the first turn runs exactly once, on the sibling's account root."""
    import infra.app_loop as app_loop
    from automation import worker_runner
    from connections import harness_usage
    from connections.account_policy import chosen_account_id
    from infra.database import get_db

    default, added, receipt = _accounts(world, monkeypatch, rollover=True, sibling="unread")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "started" and reason is None and worker is not None
    assert _workers() == [worker]
    assert (await _status(worker))["held"].startswith("Held: account limit on claude-code")
    assert _turns(world) == []  # held: no turn has been given to the model
    from subagents.cc_idle_retirement import working_thread_ids
    from subagents.cc_runtime_state import _LIVE
    # The child's process is live but not working, so it holds no live-spawn-cap slot while it waits.
    assert worker in _LIVE.ids() and worker not in working_thread_ids()
    assert chosen_account_id(worker, PROVIDER, db=get_db()) == default["id"]

    app_loop.capture(asyncio.get_running_loop())
    try:
        harness_usage.record_allowed(provider=PROVIDER, source="rate_limit_update", receipt=receipt, db=get_db())
        for _ in range(3000):  # up to 30s on a loaded runner; exits at the first turn
            if _turns(world):
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()

    assert chosen_account_id(worker, PROVIDER, db=get_db()) == added["id"]
    assert len(_turns(world)) == 1  # exactly one first turn
    assert _turns(world)[0] == added["source_home"]
    for _ in range(3):
        assert await worker_runner.drive() == 0  # no later drive starts the turn again
    await asyncio.sleep(0.3)
    assert len(_turns(world)) == 1
    # The one start settles on the worker's result once its released turn is done; none is added.
    assert [row[0] for row in starts(rule)] == ["returned"] and _workers() == [worker]
    assert _spawned_models() == {MODEL}


@pytest.mark.asyncio
async def test_an_account_that_becomes_limited_after_the_gate_reaches_the_held_branch(world, monkeypatch):
    """The race: the Default reads ready at the start gate, then reports its limit before the child's first
    turn is admitted. That turn is held on the limited account, not crashed, and the start is recorded once."""
    from automation import worker_runner
    from connections import harness_usage
    from infra.database import get_db
    from subagents import cc_spawn
    from tests.vendor_account_fixtures import default_receipt

    from connections.account_policy import set_policy
    from connections.vendor_accounts import default_account

    db = get_db()
    set_policy(world["project"], PROVIDER, rollover=False, order=[default_account(PROVIDER, db=db)["id"]],
               disabled=[], expected_revision=0, db=db)
    real_admit = cc_spawn._admit_initial_task

    def limited_just_before_admission(*args, **kwargs):
        harness_usage.record_limited(provider=PROVIDER, receipt=default_receipt(PROVIDER, db=get_db()),
                                     db=get_db(), **LIMIT)
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(cc_spawn, "_admit_initial_task", limited_just_before_admission)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "started" and reason is None and worker is not None
    assert (await _status(worker))["held"].startswith("Held: account limit on claude-code")
    assert _turns(world) == []
    assert await worker_runner.drive() == 0 and [row[0] for row in starts(rule)] == ["started"]


@pytest.mark.asyncio
async def test_rollover_off_with_a_ready_sibling_is_refused_in_todays_words(world, monkeypatch):
    """Rollover off: the limited selected account decides, so the start is refused at the gate with the
    unchanged text even though a sibling could run the model."""
    from automation import worker_runner
    from tests.rule_worker_helpers import owner_wakes

    _accounts(world, monkeypatch, rollover=False, sibling="allowed")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None
    assert reason.endswith("can't run now. Choose a model that is ready for this chat.")
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert _workers() == []
