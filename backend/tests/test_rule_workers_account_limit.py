"""A rule-started worker whose owner's account is usage-limited, through the real start path.

The catalog rows, account selection and rollover are the real ones. Only the vendor's usage
verdicts are recorded (as its limit events and usage reads would), the usage read itself is
recorded instead of run, and the worker's ``claude`` is the shared fake. The owner runs Sonnet.
"""
from __future__ import annotations

import pytest

from automation import worker_start_wait
from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, WORKFLOW, atool, make_rule, request, starts, world, worker_events,
)

PROVIDER, MODEL = "claude-code", "claude-code/sonnet"
LIMIT = dict(reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update")


def _accounts(world, monkeypatch, *, rollover: bool, sibling: str, scope_model: str | None = None,
              first: str = "limited"):
    """The Default, first in the order, and one added account in the owner's project.

    ``first`` is ``limited`` (for ``scope_model`` alone when given) or ``signed_out`` (not
    limited; rollover moves off it too). ``sibling`` is ``unread`` (discovered, usage never
    read) or ``limited``.
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
    if sibling == "limited":
        harness_usage.record_limited(provider=PROVIDER, db=db, receipt=receipt, **LIMIT)
    if first == "signed_out":
        default = set_account_state(default["id"], "signed_out", expected_revision=default["revision"])
    else:
        harness_usage.record_limited(provider=PROVIDER, receipt=default_receipt(PROVIDER, db=db), db=db,
                                     **({"scope_model": scope_model} if scope_model else {}), **LIMIT)
    set_policy(world["project"], PROVIDER, rollover=rollover, order=[default["id"], added["id"]],
               disabled=[], expected_revision=0, db=db)
    checks = []
    monkeypatch.setattr("connections.harness_usage_check.schedule_account_check",
                        lambda provider, account_id: checks.append((provider, account_id)))
    return default, added, receipt, checks


def _workers() -> list[str]:
    from infra.database import get_db

    return [row[0] for row in get_db().execute("SELECT id FROM threads WHERE spawned_by=?", (OWNER,))]


REFUSAL_END = "can't run now. Choose a model that is ready for this chat."


def _refused_plainly(reason: str) -> bool:
    """Today's refusal, word for word: a wait adds no wording of its own."""
    return reason.startswith("your model ") and reason.endswith(REFUSAL_END)


def _waiting_quietly(rule: str) -> None:
    """The start is back in the queue: no worker, no refusal, nothing told to the owner."""
    from tests.rule_worker_helpers import owner_wakes

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert (outcome, reason, worker) == ("queued", None, None)
    assert _workers() == [] and owner_wakes() == [] and worker_events() == []


FIRST = pytest.mark.parametrize("first", ["limited", "signed_out"])


@pytest.mark.asyncio
@FIRST
async def test_an_unread_sibling_waits_for_its_usage_read_then_starts_on_it(world, monkeypatch, first):
    """The first account is limited, or signed out, and a sibling is ready with its usage unread:
    rollover would move off the first account but never onto unread usage, so the start goes
    back to the queue (no worker, no slot, no refusal) and a usage read is asked of the sibling;
    once that read says allowed, the next pass starts the worker on the sibling."""
    from automation import worker_runner
    from connections import harness_usage
    from connections.account_policy import chosen_account_id
    from infra.database import get_db

    _default, added, receipt, checks = _accounts(world, monkeypatch, rollover=True, sibling="unread", first=first)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    _waiting_quietly(rule)
    assert set(checks) == {(PROVIDER, added["id"])}
    assert await worker_runner.drive() == 0  # still unread: still waiting
    _waiting_quietly(rule)

    harness_usage.record_allowed(provider=PROVIDER, source="rate_limit_update", receipt=receipt, db=get_db())
    assert await worker_runner.drive() == 1
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "started" and reason is None and _workers() == [worker]
    assert chosen_account_id(worker, PROVIDER, db=get_db()) == added["id"]
    assert worker_start_wait._waiting == {}


@pytest.mark.asyncio
@FIRST
async def test_a_sibling_read_as_limited_ends_the_wait_with_todays_refusal(world, monkeypatch, first):
    from automation import worker_runner
    from connections import harness_usage
    from infra.database import get_db
    from tests.rule_worker_helpers import owner_wakes

    _default, _added, receipt, _checks = _accounts(world, monkeypatch, rollover=True, sibling="unread", first=first)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    _waiting_quietly(rule)

    harness_usage.record_limited(provider=PROVIDER, db=get_db(), receipt=receipt, **LIMIT)
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and _refused_plainly(reason)
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert worker_start_wait._waiting == {}


@pytest.mark.asyncio
async def test_a_read_that_never_lands_ends_the_wait_with_todays_refusal(world, monkeypatch):
    """A failed read, or none, leaves the sibling unread: the wait still ends, after
    ``WAIT_SECONDS``, whether or not the workflow sets a time limit (this one sets none)."""
    from automation import worker_runner
    from tests.rule_worker_helpers import owner_wakes

    _accounts(world, monkeypatch, rollover=True, sibling="unread")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    _waiting_quietly(rule)

    monkeypatch.setattr(worker_start_wait, "WAIT_SECONDS", 0)
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and _refused_plainly(reason)
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert worker_start_wait._waiting == {}


@pytest.mark.asyncio
async def test_with_rollover_off_an_unread_sibling_is_refused_at_once(world, monkeypatch):
    from automation import worker_runner

    _default, _added, _receipt, checks = _accounts(world, monkeypatch, rollover=False, sibling="unread")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and _refused_plainly(reason)
    assert checks == [] and worker_start_wait._waiting == {}


@pytest.mark.asyncio
async def test_one_pass_decides_at_most_a_batch_of_starts_however_many_wait(world, monkeypatch):
    """Starts that wait give their slot back, but one pass still decides at most ``BATCH`` of
    them; the rest stay queued for the next pass."""
    from automation import worker_runner

    _accounts(world, monkeypatch, rollover=True, sibling="unread")
    monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live", lambda: 1)
    monkeypatch.setattr(worker_runner, "BATCH", 2)
    decided = []
    check_runs = worker_start_wait.check_runs
    monkeypatch.setattr(worker_start_wait, "check_runs", lambda *a: (decided.append(a[0]["id"]), check_runs(*a))[1])
    rule = await make_rule()
    for n in range(3):
        await request(f"pr-{n}@a1")
    assert await worker_runner.drive() == 0
    assert len(decided) == 2
    assert [outcome for outcome, *_ in starts(rule)] == ["queued"] * 3


@pytest.mark.asyncio
async def test_a_model_with_no_vendor_account_that_is_not_ready_is_refused_at_once(world, monkeypatch):
    """A cloud model run through its provider, not Claude Code or Codex, has no account to roll
    over to: not ready means today's refusal, word for word, and no wait."""
    from automation import worker_runner
    from infra.database import write_transaction
    from tests.rule_worker_helpers import owner_wakes

    native = "anthropic/claude-sonnet-4-6"
    with write_transaction(None, scope="rule_worker_native") as conn:
        conn.execute("UPDATE threads SET model=? WHERE id=?", (native, OWNER))
    row = {"id": native, "litellm_model": native, "display_name": "Sonnet 4.6", "connection": "anthropic",
           "is_local": False, "status": "unavailable", "status_reason": "missing_context_window"}
    monkeypatch.setattr("llm.model_status.scoped_status_rows", lambda **_scope: [row])
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and _refused_plainly(reason)
    assert reason in owner_wakes()[0] and worker_start_wait._waiting == {}


OTHER = "rw-other-owner"


async def _rule_for(owner: str, request_value: str) -> str:
    created = await atool(owner, operation="create", connector="qa", event_type="request",
                          action="start_worker", workflow_id=WORKFLOW, request_identity_fact="request",
                          match=[{"field": "request", "op": "eq", "value": request_value}])
    assert isinstance(created, dict), created
    return created["id"]


@pytest.mark.asyncio
async def test_a_waiting_start_gives_its_slot_to_a_younger_start_that_can_run(world, monkeypatch):
    """With one slot free, the older start waits on an unread account and the younger start,
    another owner's on a model that runs (the Default is limited for Sonnet alone), starts in
    the same pass."""
    from automation import worker_runner
    from infra.database import write_transaction

    _accounts(world, monkeypatch, rollover=True, sibling="unread", scope_model=MODEL)
    monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live", lambda: 1)
    with write_transaction(None, scope="rule_worker_other_owner") as conn:
        conn.execute("INSERT INTO threads (id, user_id, title, model, project_id, created_at, updated_at) "
                     "VALUES (?, 'user-1', 'Other', 'claude-code/opus', ?, 't', 't')", (OTHER, world["project"]))
    staging = world["root"] / ".threads" / OTHER / "staging" / "req"
    staging.mkdir(parents=True)
    (staging / "change.diff").write_text("+ y = 2\n")
    older, younger = await _rule_for(OWNER, "old"), await _rule_for(OTHER, "new")
    await request("old")
    await request("new")

    assert await worker_runner.drive() == 1
    assert starts(older)[0][0] == "queued" and starts(older)[0][3] is None
    [(outcome, _reason, _identity, worker)] = starts(younger)
    assert outcome == "started" and worker is not None
    assert await worker_runner.drive() == 0  # no slot free now: the older start still waits
    assert starts(older)[0][0] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("rollover", [True, False])
async def test_every_account_limited_is_refused_visibly_and_starts_no_worker(world, monkeypatch, rollover):
    from automation import worker_runner
    from tests.rule_worker_helpers import owner_wakes

    _accounts(world, monkeypatch, rollover=rollover, sibling="limited")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0

    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and _refused_plainly(reason)
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
