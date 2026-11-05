"""A workflow's ``worker`` block reaches its rule-started worker: effort, turn cap, time limit and context."""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, WORKFLOW, atool, make_rule, owner_wakes, request, settle_all, starts, world, worker_events,
)

_BLOCK = """  worker:
    effort: high
    max_turns: 200
    time_limit_minutes: 30
    min_context_tokens: 100000
    profiles:
      main: {min_context_tokens: 1000000}
"""
_PLAIN = {"model", "confinement", "read_paths", "title"}


def _workflow_block(world, block: str | None) -> None:
    sidecar = world["root"] / "skills" / WORKFLOW / "corsia.yaml"
    text = sidecar.read_text().split("  worker:\n")[0]
    sidecar.write_text(text + (block or ""))


def _owner_model(model: str) -> None:
    from infra.database import write_transaction

    with write_transaction(None, scope="rule_worker_settings") as db:
        db.execute("UPDATE threads SET model=? WHERE id=?", (model, OWNER))
        # The provider reports 1,000,000 for opus; CORSIA's effective window is its 262,144 default.
        db.execute("DELETE FROM model_profiles WHERE model_id='claude-code/opus'")


def _variant_row(*, enabled: int = 1, available: int = 1, window: int = 1_000_000) -> None:
    """The catalog's own ``opus[1m]`` row (discovery stores one for a selector the CLI lists)."""
    from infra.database import write_transaction
    from vendor_account_fixtures import signed_in_default

    with write_transaction(None, scope="rule_worker_variant") as db:
        db.execute(
            "INSERT INTO models (id, harness, provider, provider_model_id, litellm_model, display_name, backend, "
            "source, enabled, available, context_window, supports_tools, metadata_json, created_at, updated_at) "
            "SELECT 'claude-code/opus[1m]', harness, provider, 'opus[1m]', 'claude-code/opus[1m]', "
            "'opus[1m]', backend, source, ?, ?, ?, supports_tools, metadata_json, created_at, updated_at "
            "FROM models WHERE id='claude-code/opus'", (enabled, available, window))
    signed_in_default("claude-code")  # the Default account's discovery lists every stored row


@pytest.fixture
def spawned(monkeypatch):
    """Every ``_spawn`` kwargs the runner passes; the real spawn still runs."""
    from tools import delegate_task

    real = delegate_task._spawn
    seen: list[dict] = []

    async def spy(parent, owner, task, kwargs, **more):
        seen.append(dict(kwargs))
        return await real(parent, owner, task, kwargs, **more)

    monkeypatch.setattr(delegate_task, "_spawn", spy)
    return seen


def _argv(world) -> list[str]:
    """The fake ``claude``'s command line, written when its process starts."""
    path = world["reply"].parent / "reply.argv"
    for _ in range(100):
        if path.exists() and path.read_text():
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError("the worker's process never started")


def _kept(rule: str) -> dict:
    """The start's kept check (what the runner recorded when it made the worker)."""
    from infra.database import get_db

    return json.loads(get_db().execute(
        "SELECT run_input FROM event_route_decisions WHERE rule_id=?", (rule,)).fetchone()[0])


@pytest.mark.asyncio
async def test_no_worker_block_spawns_as_before(world, spawned):
    from automation import worker_runner

    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert set(spawned[0]) == _PLAIN
    argv = _argv(world)
    assert not {"--max-turns", "--effort"} & set(argv)
    assert _kept(rule)["settings"]["max_turns"] is None


@pytest.mark.asyncio
async def test_base_settings_reach_the_worker_and_are_recorded(world, spawned):
    from automation import worker_runner

    _workflow_block(world, _BLOCK)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert spawned[0]["effort"] == "high" and spawned[0]["max_turns"] == 200
    argv = _argv(world)
    assert argv[argv.index("--max-turns") + 1] == "200"
    assert argv[argv.index("--effort") + 1] == "high"
    settings = _kept(rule)["settings"]
    assert (settings["model"], settings["effort"], settings["max_turns"], settings["time_limit_minutes"],
            settings["profile"]) == ("claude-code/sonnet", "high", 200, 30, None)
    assert settings["started_at"]


@pytest.mark.asyncio
async def test_a_profile_overrides_only_its_keys_and_runs_the_larger_context_variant(world, spawned):
    from automation import worker_runner

    _owner_model("claude-code/opus")
    _variant_row()
    _workflow_block(world, _BLOCK)
    rule = await make_rule(worker_profile="main")
    await request()
    assert await worker_runner.drive() == 1
    assert spawned[0]["model"] == "claude-code/opus[1m]"
    assert (spawned[0]["effort"], spawned[0]["max_turns"]) == ("high", 200)
    argv = _argv(world)
    assert argv[argv.index("--model") + 1].endswith("[1m]")
    settings = _kept(rule)["settings"]
    assert (settings["model"], settings["profile"], settings["min_context_tokens"],
            settings["time_limit_minutes"]) == ("claude-code/opus[1m]", "main", 1_000_000, 30)


@pytest.mark.asyncio
async def test_a_window_that_fits_runs_the_model_as_it_is(world, spawned):
    from automation import worker_runner

    _owner_model("claude-code/opus")
    _workflow_block(world, _BLOCK)
    await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert spawned[0]["model"] == "claude-code/opus"


@pytest.mark.asyncio
async def test_a_window_too_small_with_no_larger_variant_is_refused_in_plain_words(world, spawned):
    from automation import worker_runner

    _workflow_block(world, _BLOCK)  # sonnet reports 200,000 itself: no larger version exists
    rule = await make_rule(worker_profile="main")
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and spawned == []
    assert "this workflow needs a model with at least 1,000,000 tokens of context; your model " in reason
    assert "has 200,000 tokens and no larger-context version of it can run now. " in reason
    assert reason.endswith("Choose a model with a larger context for this chat.")
    assert reason in owner_wakes()[0] and [kind for kind, _ in worker_events()] == ["worker.no_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [{"enabled": 0}, {"available": 0}, {"window": 500_000}])
async def test_a_larger_variant_that_is_off_unavailable_or_too_small_is_refused(world, spawned, row):
    from automation import worker_runner

    _owner_model("claude-code/opus")
    _variant_row(**row)  # a window of 500,000: the variant's own row counts, not the base's 1,000,000
    _workflow_block(world, _BLOCK)
    rule = await make_rule(worker_profile="main")
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None and spawned == []
    assert "needs a model with at least 1,000,000 tokens of context" in reason
    assert reason.endswith("Choose a model with a larger context for this chat.")


@pytest.mark.asyncio
async def test_an_effort_the_model_does_not_list_is_refused_naming_what_it_takes(world, spawned):
    from automation import worker_runner

    _workflow_block(world, "  worker:\n    effort: ultra\n")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, *_rest)] = starts(rule)
    assert outcome == "refused" and spawned == []
    assert "runs at ultra effort" in reason and "takes: low, medium, high, xhigh, max" in reason


@pytest.mark.asyncio
async def test_an_effort_on_a_model_that_reports_no_levels_passes_through(world, spawned, monkeypatch):
    from automation import worker_runner

    from llm import model_status

    real = model_status.scoped_status_rows

    def no_levels(**scope):
        return [{**row, "capabilities": {**(row.get("capabilities") or {}), "reasoning_config": None}}
                for row in real(**scope)]

    monkeypatch.setattr(model_status, "scoped_status_rows", no_levels)
    _workflow_block(world, "  worker:\n    effort: high\n")
    await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert spawned[0]["effort"] == "high"


@pytest.mark.asyncio
async def test_an_unknown_profile_is_refused_when_the_rule_is_written_and_when_it_starts(world, spawned):
    from automation import worker_runner

    _workflow_block(world, _BLOCK)
    refused = await atool(OWNER, operation="create", connector="qa", event_type="request", action="start_worker",
                          workflow_id=WORKFLOW, request_identity_fact="request", worker_profile="nope",
                          match=[{"field": "request", "op": "present"}])
    assert isinstance(refused, str) and "no profile 'nope'" in refused and "main" in refused
    rule = await make_rule(worker_profile="main")
    _workflow_block(world, None)  # the workflow changed after the rule was written
    await request()
    assert await worker_runner.drive() == 0
    [(outcome, reason, *_rest)] = starts(rule)
    assert outcome == "refused" and spawned == []
    assert reason == ("this rule starts the worker-flow workflow with the worker profile main, which "
                      "that workflow does not have. Change the rule's profile or add it to the workflow.")
    assert reason in owner_wakes()[0]


@pytest.mark.asyncio
async def test_worker_profile_goes_only_with_start_worker(world):
    refused = await atool(OWNER, operation="create", connector="qa", event_type="request", action="deliver",
                          worker_profile="main", match=[{"field": "request", "op": "present"}])
    assert isinstance(refused, str) and "worker_profile go only with action start_worker" in refused


@pytest.mark.asyncio
async def test_a_harness_with_no_turn_cap_gets_none_and_the_record_says_so(world, monkeypatch):
    from automation import worker_runner
    from infra.database import write_transaction
    from infra.db_async import run_in_db
    from subagents import codex_confinement
    from tools import delegate_task

    def to_codex():
        with write_transaction(None, scope="rule_worker_model") as db:
            db.execute("UPDATE threads SET model='codex/gpt-5-codex' WHERE id=?", (OWNER,))

    await run_in_db(to_codex)
    _workflow_block(world, "  worker:\n    effort: high\n    max_turns: 200\n    time_limit_minutes: 30\n")
    seen: list[dict] = []

    async def spawn(_parent, _owner, _task, kwargs, rule_start=None):
        seen.append(dict(kwargs))
        await rule_start("worker-x")
        return {"thread_id": "worker-x"}

    monkeypatch.setattr(codex_confinement, "verify_version", AsyncMock())
    monkeypatch.setattr(delegate_task, "_spawn", spawn)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert seen[0]["effort"] == "high" and "max_turns" not in seen[0]
    settings = _kept(rule)["settings"]
    assert settings["max_turns"] is None and "no per-run turn cap" in settings["max_turns_note"]


def _backdate(rule: str, minutes: int) -> None:
    from datetime import datetime, timedelta, timezone

    from infra.database import write_transaction

    kept = _kept(rule)
    kept["settings"]["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with write_transaction(None, scope="rule_worker_backdate") as db:
        db.execute("UPDATE event_route_decisions SET run_input=? WHERE rule_id=?", (json.dumps(kept), rule))


@pytest.mark.asyncio
async def test_a_worker_past_its_limit_is_stopped_and_settles_no_result_once(world):
    from automation import worker_runner

    world["reply"].write_text("HANG")
    _workflow_block(world, "  worker:\n    time_limit_minutes: 30\n")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    assert await worker_runner.drive() == 0
    assert starts(rule)[0][0] == "started"  # under its limit: left running
    _backdate(rule, 31)
    await worker_runner.drive()
    [(outcome, reason, _identity, _worker)] = starts(rule)
    assert (outcome, reason) == ("no_result", "the agent ran past its 30-minute limit")
    await worker_runner.drive()
    await settle_all()
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert "the agent ran past its 30-minute limit" in owner_wakes()[0]


@pytest.mark.asyncio
async def test_a_worker_settled_by_its_time_limit_names_the_model_it_was_spawned_on(world, monkeypatch):
    import events.intake as intake
    from automation import worker_runner

    admitted, real = [], intake.admit_event

    async def capture(**kwargs):
        admitted.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(intake, "admit_event", capture)
    world["reply"].write_text("HANG")
    _workflow_block(world, "  worker:\n    time_limit_minutes: 30\n")
    rule = await make_rule()
    await request()
    await worker_runner.drive()
    _backdate(rule, 31)
    await worker_runner.drive()
    [event] = [kwargs for kwargs in admitted if kwargs["event_type"] == "worker.no_result"]
    assert event["facts"]["model"] == "claude-code/sonnet"


async def _worker_past_its_limit(world) -> str:
    """A rule whose worker is running (it never answers) and is 31 minutes into a 30-minute limit."""
    from automation import worker_runner

    world["reply"].write_text("HANG")
    _workflow_block(world, "  worker:\n    time_limit_minutes: 30\n")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    _backdate(rule, 31)
    return rule


@pytest.mark.asyncio
async def test_a_worker_with_no_running_turn_past_its_limit_is_not_settled_and_holds_its_identity(world):
    from automation import worker_runner
    from infra.database import write_transaction

    rule = await _worker_past_its_limit(world)
    worker = starts(rule)[0][3]
    with write_transaction(None, scope="rule_worker_held") as db:  # its turn is held, not running
        db.execute("UPDATE active_turns SET status='waiting_retry', retry_at='2099-01-01T00:00:00+00:00' "
                   "WHERE thread_id=?", (worker,))
    await worker_runner.drive()
    assert starts(rule)[0][:2] == ("started", None)
    await request()  # a later event for the same request must not start a second worker
    await worker_runner.drive()
    assert [row[3] for row in starts(rule) if row[3]] == [worker] and worker_events() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["stale_active", "already_terminal", "no_active_turn", "raises"])
async def test_a_stop_that_does_not_end_the_turn_settles_nothing(world, monkeypatch, outcome):
    from automation import worker_runner
    from ws import stop_control

    rule = await _worker_past_its_limit(world)
    worker = starts(rule)[0][3]

    async def stop(sid, turn_id, **_kw):
        if outcome == "raises":
            raise RuntimeError("the stop failed")
        return stop_control.StopReceipt(sid, outcome, turn_id)

    monkeypatch.setattr(stop_control, "stop_turn_for_cause", stop)
    await worker_runner.drive()
    assert starts(rule)[0][:2] == ("started", None) and starts(rule)[0][3] == worker
    assert worker_events() == []


@pytest.mark.asyncio
async def test_the_time_limit_stop_goes_through_the_one_system_stop_without_the_plan_limit_cause(world, monkeypatch):
    from automation import worker_runner
    from ws import stop_control

    rule = await _worker_past_its_limit(world)
    real, seen = stop_control.stop_turn_for_cause, []

    async def spy(sid, turn_id, **kw):
        seen.append(kw)
        return await real(sid, turn_id, **kw)

    monkeypatch.setattr(stop_control, "stop_turn_for_cause", spy)
    await worker_runner.drive()
    assert seen == [{"plan_limit": False}]
    assert starts(rule)[0][:2] == ("no_result", "the agent ran past its 30-minute limit")


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["MAX_TURNS_ERROR", "MAX_TURNS_QUIET"])
async def test_an_exhausted_turn_cap_settles_no_result_with_a_named_reason(world, reply):
    from automation import worker_runner

    world["reply"].write_text(reply)
    _workflow_block(world, "  worker:\n    max_turns: 3\n")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    await settle_all()
    [(outcome, reason, _identity, _worker)] = starts(rule)
    assert (outcome, reason) == ("no_result", "the agent reached its turn limit")
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert "the agent reached its turn limit" in owner_wakes()[0]
