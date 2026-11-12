"""A rule-started worker whose owner's account is usage-limited, through the real start path.

The catalog rows, account selection, rollover and held-input gate are the real ones. Only the
vendor's usage verdicts are recorded (as its limit events would) and the worker's model call is
stubbed. The owner runs a Codex model; the Claude Code cases are in
``test_rule_workers_account_limit_cc.py``.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, make_rule, request, starts, world, worker_events,
)

PROVIDER, MODEL = "codex", "codex/gpt-5-codex"
LIMIT = dict(reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update")


def _accounts(world, monkeypatch, *, rollover: bool, sibling: str):
    """The Default (limited) and one added account in the owner's project, with the owner on Codex.

    ``sibling`` is ``unread`` (discovered, usage never read), ``allowed`` or ``limited``.
    """
    from connections import harness_usage
    from connections.account_evidence import mark_discovery
    from connections.account_policy import set_policy
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, default_account, set_account_state
    from infra.database import get_db, write_transaction
    from tests.vendor_account_fixtures import default_receipt

    with write_transaction(None, scope="rule_worker_codex_owner") as conn:
        conn.execute("UPDATE threads SET model=? WHERE id=?", (MODEL, OWNER))
    db = get_db()
    default = default_account(PROVIDER, db=db)
    added = set_account_state(add_account(PROVIDER, "Added")["id"], "signed_in", expected_revision=0)
    (Path(added["source_home"]) / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access_token": "synthetic"}}))
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
    checks = []
    monkeypatch.setattr("connections.harness_usage_check.schedule_account_check",
                        lambda provider, account_id: checks.append((provider, account_id)))
    return default, added, receipt, checks


def _workers() -> list[str]:
    from infra.database import get_db

    return [row[0] for row in get_db().execute("SELECT id FROM threads WHERE spawned_by=?", (OWNER,))]


async def _status(worker: str) -> dict:
    from tools.delegation import DelegateStatusTool

    return json.loads(await DelegateStatusTool().execute(thread_id=worker, _thread_id=OWNER))


REFUSAL = "your model {name} can't run now. Choose a model that is ready for this chat."


def _spawned_models() -> set[str]:
    from infra.database import get_db

    return {row[0] for row in get_db().execute("SELECT model FROM threads WHERE spawned_by=?", (OWNER,))}


@pytest.fixture
def codex_worker(monkeypatch):
    """A Codex worker's start needs its connection's version check; nothing else about it is stubbed."""
    monkeypatch.setattr("subagents.codex_confinement.verify_version", AsyncMock())


@pytest.mark.asyncio
async def test_an_unread_sibling_holds_the_workers_first_turn_then_releases_it_onto_the_sibling(
        world, monkeypatch, codex_worker):
    """The selected account is limited and a sibling is ready with its usage unread: the start is not
    refused, the first turn is held (its reason shows in delegate_status), a usage check is asked of
    the sibling, and no second start fires; when that read says allowed the turn moves to the sibling."""
    import infra.app_loop as app_loop
    from automation import worker_runner
    from connections import account_moves, harness_usage
    from connections.account_policy import chosen_account_id
    from infra.database import get_db

    default, added, receipt, checks = _accounts(world, monkeypatch, rollover=True, sibling="unread")
    monkeypatch.setattr(account_moves, "_publish", lambda _event: None)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "started" and reason is None and worker is not None
    assert _workers() == [worker]
    reset = datetime.fromisoformat(LIMIT["resets_at"]).astimezone()
    expected = f"Held: account limit on codex, resets {reset:%b} {reset.day}, {reset:%Y at %-I:%M %p %Z}."
    assert (await _status(worker))["held"] == expected
    assert chosen_account_id(worker, PROVIDER, db=get_db()) == default["id"]
    assert {(PROVIDER, added["id"])} == set(checks)
    assert _spawned_models() == {MODEL}

    admitted = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", admitted)
    app_loop.capture(asyncio.get_running_loop())
    try:
        db = get_db()
        harness_usage.record_allowed(provider=PROVIDER, source="rate_limit_update", receipt=receipt, db=db)
        for _ in range(3000):  # up to 30s on a loaded runner; exits at the first admission
            if admitted.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()

    batch = admitted.await_args.args[0]
    assert type(batch).__name__ == "ClaimedBatch" and batch.thread_id == worker
    assert chosen_account_id(worker, PROVIDER, db=get_db()) == added["id"]
    assert await worker_runner.drive() == 0  # nothing is started again
    assert [row[0] for row in starts(rule)] == ["started"] and _workers() == [worker]
    assert _spawned_models() == {MODEL}


@pytest.mark.asyncio
@pytest.mark.parametrize("rollover", [True, False])
async def test_every_account_limited_is_refused_visibly_and_starts_no_worker(world, monkeypatch, codex_worker, rollover):
    from automation import worker_runner
    from tests.rule_worker_helpers import owner_wakes

    _accounts(world, monkeypatch, rollover=rollover, sibling="limited")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None
    assert reason == REFUSAL.format(name="GPT-5 Codex") or reason.endswith(
        "can't run now. Choose a model that is ready for this chat.")
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert _workers() == []


def _variant_row() -> None:
    """The catalog's own ``opus[1m]`` row, which the Default account's discovery lists."""
    from infra.database import write_transaction
    from vendor_account_fixtures import signed_in_default

    with write_transaction(None, scope="rule_worker_variant") as db:
        db.execute(
            "INSERT INTO models (id, harness, provider, provider_model_id, litellm_model, display_name, backend, "
            "source, enabled, available, context_window, supports_tools, metadata_json, created_at, updated_at) "
            "SELECT 'claude-code/opus[1m]', harness, provider, 'opus[1m]', 'claude-code/opus[1m]', "
            "'opus[1m]', backend, source, 1, 1, 1000000, supports_tools, metadata_json, created_at, updated_at "
            "FROM models WHERE id='claude-code/opus'")
        db.execute("UPDATE threads SET model='claude-code/opus' WHERE id=?", (OWNER,))
        db.execute("DELETE FROM model_profiles WHERE model_id='claude-code/opus'")
    signed_in_default("claude-code")


@pytest.mark.asyncio
async def test_the_extended_window_variant_that_cannot_run_still_refuses_visibly(world, monkeypatch):
    """A workflow that needs a 1M window runs the model's ``[1m]`` variant or refuses; with the
    variant's only account limited (the base model is not) it refuses in the same words, and no worker starts."""
    from automation import worker_runner
    from connections import harness_usage
    from infra.database import get_db
    from tests.rule_worker_helpers import WORKFLOW, owner_wakes
    from tests.vendor_account_fixtures import default_receipt

    _variant_row()
    sidecar = world["root"] / "skills" / WORKFLOW / "corsia.yaml"
    sidecar.write_text(sidecar.read_text() + "  worker:\n    min_context_tokens: 1000000\n")
    harness_usage.record_limited(provider="claude-code", receipt=default_receipt("claude-code"), db=get_db(),
                                 scope_model="claude-code/opus[1m]", **LIMIT)  # the variant alone is limited
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None
    assert "no larger-context version of it can run now. " in reason
    assert reason.endswith("Choose a model with a larger context for this chat.")
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert _workers() == []
