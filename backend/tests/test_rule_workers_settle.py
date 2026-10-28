"""A rule-started worker settles once, as one ``worker.result`` or ``worker.no_result`` (R24, §4.2).

Through the real runtime with a fake ``claude``; a worker is killed for real, and the
owner's ``run_script`` rules run through the real script runner with only the sandboxed
process recorded.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, fenced, make_rule, owner_script, owner_wakes, request, run_scripts, settle_all,
    starts, world, worker_events,
)


async def _script_rule(script_id: str, kind: str) -> str:
    created = await atool(OWNER, operation="create", connector="worker", event_type=kind,
                          action="run_script", script_id=script_id,
                          match=[{"field": "requester", "op": "present"}])
    assert isinstance(created, dict), created
    return created["id"]


# 3 -- a worker killed mid-run: worker.no_result, the identity released, a re-request starts one.
@pytest.mark.asyncio
async def test_a_killed_worker_returns_no_result_and_releases_its_request(world):
    from automation import worker_runner
    from subagents.cc_runtime_state import _LIVE

    world["reply"].write_text("HANG")
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    worker = starts(rule)[0][3]
    for _ in range(200):
        live = _LIVE.get(worker)
        if live is not None and live.session._proc is not None:
            break
        await asyncio.sleep(0.02)
    live.session._proc.kill()
    await settle_all()
    [(outcome, reason, identity, _worker)] = starts(rule)
    assert outcome == "no_result" and reason.startswith("the agent's turn failed") and identity
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert len(owner_wakes()) == 1 and world["driven"] == [OWNER]

    world["reply"].write_text(fenced({"findings": []}))
    await request()
    await request()
    await settle_all()
    assert [row[:2] for row in starts(rule)][1:] == [("returned", None), ("skipped", "duplicate request")]


# 3 (restart) -- a start a restart cut short before its worker was made returns no result.
@pytest.mark.asyncio
async def test_a_start_cut_short_by_a_restart_returns_no_result(world):
    from events import worker_starts
    from infra.database import write_transaction

    rule = await make_rule()
    await request()
    with write_transaction(None, scope="rule_worker_claim") as db:
        decision = db.execute("SELECT id FROM event_route_decisions WHERE rule_id=?", (rule,)).fetchone()[0]
        assert worker_starts.claim(db, decision) is not None
    await settle_all()
    assert starts(rule)[0][:2] == ("no_result", "a restart cut the start short")
    assert len(owner_wakes()) == 1


# 3 (restart) -- a worker made but cut short before its first input or turn settles once.
@pytest.mark.asyncio
async def test_a_worker_cut_short_before_its_first_turn_returns_no_result_once(world):
    from events import worker_starts
    from infra.database import write_transaction

    rule = await make_rule()
    await request()
    with write_transaction(None, scope="rule_worker_claim") as db:
        decision = db.execute("SELECT id FROM event_route_decisions WHERE rule_id=?", (rule,)).fetchone()[0]
        assert worker_starts.claim(db, decision) is not None
        worker_starts.made(db, decision, "rw-cut-short", "{}")
    await settle_all()
    await settle_all()
    assert starts(rule) == [("no_result", "a restart cut the start short", "pr-1@a1", "rw-cut-short")]
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"] and len(owner_wakes()) == 1


# 3 (restart) -- a start cut short, made or not, removes its inputs and reports what its row
# kept: the rule in the event's trace (so loop detection sees it) and the workflow.
@pytest.mark.asyncio
async def test_an_orphaned_start_removes_its_inputs_and_carries_its_trace(world, monkeypatch):
    from automation.worker_inputs import start_dir
    from events import intake, worker_starts
    from infra.database import write_transaction

    real, admitted = intake.admit_event, []

    async def recorded(**kwargs):
        admitted.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(intake, "admit_event", recorded)
    rule = await make_rule()
    await request("pr-1@a1")
    await request("pr-2@a1")
    check = json.dumps({"workflow": "worker-flow@3", "trace": ["rule_up", rule]})
    with write_transaction(None, scope="rule_worker_claim") as db:
        unmade, made = (row[0] for row in db.execute(
            "SELECT id FROM event_route_decisions WHERE rule_id=? ORDER BY id", (rule,)))
        for decision in (unmade, made):
            assert worker_starts.claim(db, decision) is not None
        worker_starts.made(db, made, "rw-cut-short", check)
    dirs = [start_dir(world["folder"], decision) for decision in (unmade, made)]
    for path in dirs:
        (path / "head").mkdir(parents=True)
    await settle_all()
    assert [row[:2] for row in starts(rule)] == [("no_result", "a restart cut the start short")] * 2
    assert not any(path.exists() for path in dirs)
    assert {(item["trace"], item["facts"]["workflow"]) for item in admitted
            if item["event_type"] == "worker.no_result"} == {
        ((rule,), "worker-flow"), (("rule_up", rule), "worker-flow@3")}


# The owner reads a classified reason; the exception's own text stays in the log.
@pytest.mark.asyncio
async def test_the_owner_wake_and_event_carry_no_exception_text(world, monkeypatch):
    from automation import worker_inputs
    from automation.worker_settle import verdict
    from events import intake
    from infra.database import get_db

    def broken(*_args):
        raise RuntimeError("secret /private/path detail")

    real, admitted = intake.admit_event, []

    async def recorded(**kwargs):
        admitted.append(kwargs["facts"])
        return await real(**kwargs)

    monkeypatch.setattr(worker_inputs, "brief", broken)
    monkeypatch.setattr(intake, "admit_event", recorded)
    rule = await make_rule()
    await request()
    await settle_all()
    [(outcome, reason, *_)] = starts(rule)
    [wake] = owner_wakes()
    stored = [row[0] for row in get_db().execute("SELECT content FROM messages")]
    assert (outcome, reason) == ("refused", "the start failed")
    assert "A rule couldn’t start worker-flow: the start failed\n" in wake
    assert all("secret" not in text and "RuntimeError" not in text
               for text in (reason, *stored, *map(json.dumps, admitted)))
    turn = {"id": "t", "status": "error", "error": "Traceback (most recent call last): secret"}
    assert verdict(turn, None, {}) == ("no_result", {"reason": "the agent's turn failed"})
    assert verdict({**turn, "error": "process_exited"}, None, {})[1] == {
        "reason": "the agent's turn failed (process_exited)"}


# Each settled start is recorded once as a product event: rule, workflow, outcome, waited.
@pytest.mark.asyncio
async def test_each_settled_start_emits_one_product_event(world, monkeypatch):
    from automation import worker_settle

    emitted = []
    monkeypatch.setattr(worker_settle, "emit_product_event",
                        lambda category, action, **kw: emitted.append((category, action, kw)))
    rule = await make_rule()
    await request()
    await settle_all()
    await settle_all()
    [(category, action, kw)] = emitted
    assert (category, action, kw["thread_id"]) == ("event_rule", "start_settled", OWNER)
    props = kw["properties"]
    assert (props["rule_id"], props["outcome"], props["waited_for_capacity"]) == (rule, "returned", False)
    assert props["workflow"].startswith("worker-flow@") and set(props) == {
        "rule_id", "workflow", "outcome", "waited_for_capacity"}


# A worker answers its owner only as a worker.* event while its start is open; once it has
# settled, a later turn (the owner messaged it) is an ordinary child's completion.
@pytest.mark.asyncio
async def test_a_settled_workers_later_turn_wakes_its_owner_as_an_ordinary_child(world, monkeypatch):
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    from infra.queries import insert_message
    from infra.thread_driver import PARENT_COMPLETION_HEADING, _parent_completion

    def completions() -> list[str]:
        return [row[0] for row in get_db().execute(
            "SELECT content FROM messages WHERE thread_id=? AND json_valid(metadata) "
            "AND json_extract(metadata,'$.completion')=1", (OWNER,))]

    def completion(turn_id: str) -> str | None:
        with write_transaction(None, scope="rule_worker_parent_completion") as db:
            return _parent_completion(db, turn_id, worker)

    poke = worker_runner.poke
    monkeypatch.setattr(worker_runner, "poke", lambda **_: None)  # hold the settle back
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    worker = starts(rule)[0][3]
    for _ in range(300):
        turn = get_db().execute(
            "SELECT id FROM active_turns WHERE thread_id=? AND status='completed'", (worker,)).fetchone()
        if turn is not None:
            break
        await asyncio.sleep(0.02)
    assert turn is not None, "the worker's turn never completed"
    assert starts(rule)[0][0] == "started"
    assert await asyncio.to_thread(completion, turn[0]) is None and completions() == []

    monkeypatch.setattr(worker_runner, "poke", poke)
    await settle_all()
    assert starts(rule)[0][0] == "returned" and completions() == []

    def start_later_turn() -> None:
        with write_transaction(None, scope="test_later_turn") as db:
            db.execute(
                "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
                "VALUES ('owner-asked',?,0,'claude-code/sonnet','running','2099-01-01','boot')", (worker,),
            )
            insert_message(worker, "assistant", "Answered the owner.", metadata={"turn_id": "owner-asked"}, db=db)

    def finish_later_turn() -> None:
        with write_transaction(None, scope="test_finish_turn") as db:
            db.execute("UPDATE active_turns SET status='completed' WHERE id='owner-asked'")

    await asyncio.to_thread(start_later_turn)
    assert await asyncio.to_thread(completion, "owner-asked") is None and completions() == []
    await asyncio.to_thread(finish_later_turn)
    assert await asyncio.to_thread(completion, "owner-asked") == OWNER
    assert completions() == [
        f"{PARENT_COMPLETION_HEADING}\n\nfrom an untrusted agent; contains outside content\n\nAnswered the owner."
    ]


# 4 -- schema-invalid output: no_result {reason: invalid}; the same request starts nothing, a bumped attempt starts one.
@pytest.mark.asyncio
async def test_an_invalid_result_keeps_its_request_and_a_new_attempt_starts(world, monkeypatch):
    world["reply"].write_text(fenced({"findings": "not a list"}))
    seen = await _script_rule(owner_script(world, monkeypatch, "seen.sh"), "no_result")
    rule = await make_rule()
    await request()
    await settle_all()
    await run_scripts()
    assert starts(rule)[0][:2] == ("invalid", "invalid")
    [facts] = [item["facts"] for item in world["script_inputs"]]
    assert facts["reason"] == "invalid" and facts["valid"] is False and "findings" in facts["detail"]
    assert starts(seen)[0][0] == "ran"

    await request()
    world["reply"].write_text(fenced({"findings": []}))
    await request("pr-1@a2")
    await settle_all()
    assert [row[:3] for row in starts(rule)] == [
        ("invalid", "invalid", "pr-1@a1"), ("skipped", "duplicate request", None),
        ("returned", None, "pr-1@a2")]


# 5 -- a fenced, text-wrapped payload is accepted with its defaults and coercions applied.
@pytest.mark.asyncio
async def test_a_wrapped_payload_is_accepted_with_defaults_and_coercions(world, monkeypatch):
    world["reply"].write_text(fenced({"findings": [
        {"title": "Off by one", "severity": "weird", "line": "7"},
        {"title": "Typo", "severity": "minor", "blocking": True},
    ]}))
    got = await _script_rule(owner_script(world, monkeypatch, "got.sh"), "result")
    rule = await make_rule()
    await request()
    await settle_all()
    await run_scripts()
    assert starts(rule)[0][0] == "returned" and starts(got)[0][0] == "ran"
    [event] = world["script_inputs"]
    facts = event["facts"]
    assert event["type"] == "worker.result" and facts["valid"] is True
    assert json.loads(facts["result"]) == {"findings": [
        {"title": "Off by one", "severity": "nit", "line": 7, "blocking": False},
        {"title": "Typo", "severity": "minor", "blocking": True},
    ]}
    assert facts["workflow"].startswith("worker-flow@") and facts["request_identity"] == "pr-1@a1"
    assert facts["source"] == "qa.request" and facts["requester"] == OWNER
    assert owner_wakes() == []  # a result is never a wake for the rule owner


# 7 -- nothing usable came back: an owner whose run_script took worker.no_result gets no wake.
@pytest.mark.asyncio
async def test_an_owner_whose_script_took_no_result_is_not_woken(world, monkeypatch):
    world["reply"].write_text("no json here")
    await _script_rule(owner_script(world, monkeypatch, "took.sh"), "no_result")
    rule = await make_rule()
    await request()
    await settle_all()
    await run_scripts()
    assert starts(rule)[0][:2] == ("invalid", "invalid")
    assert len(world["script_inputs"]) == 1
    assert owner_wakes() == [] and world["driven"] == []


@pytest.mark.asyncio
async def test_an_owner_without_such_a_script_is_woken_once(world):
    world["reply"].write_text("no json here")
    rule = await make_rule()
    await request()
    await settle_all()
    await settle_all()
    assert starts(rule)[0][:2] == ("invalid", "invalid")
    [wake] = owner_wakes()
    assert "worker-flow" in wake and "invalid" in wake and world["driven"] == [OWNER]


# 6 -- a valid result on a request's artifact reaches that artifact's owner through rule 0.
@pytest.mark.asyncio
async def test_a_result_on_an_artifact_reaches_its_owner_through_rule_0(world, monkeypatch):
    from tests.event_routing_helpers import mk_session, pr_payload, register_pr, route

    repo = "o/r"
    monkeypatch.setattr("events.connectors.github_notifications_feed.tracked_github_repositories",
                        lambda project_id=None: frozenset({repo}) if project_id == world["project"]
                        else frozenset())
    monkeypatch.setattr("events.connectors.github_notifications_feed.projects_tracking_github_repository",
                        lambda repository: frozenset({world["project"]}) if repository == repo
                        else frozenset())
    pr_owner = mk_session("rw-pr-owner", project_id=world["project"])
    register_pr(repo, 7, pr_owner)
    created = await atool(OWNER, operation="create", connector="github", event_type="pr.synchronize",
                          action="start_worker", workflow_id="worker-flow", request_identity_fact="sha",
                          match=[{"field": "sha", "op": "present"}])
    assert isinstance(created, dict), created
    from infra.database import get_db

    # Only the PR's own webhook wake is a live send; it is recorded instead of driven.
    with patch("events.delivery.send_thread_input", AsyncMock()):
        await route("webhook.github.pr_synchronize", pr_payload(repo, 7, action="synchronize"))
        await settle_all()
    assert starts(created["id"])[0][:3] == ("returned", None, "abc123")
    assert [kind for kind, _ in worker_events()] == ["worker.result"]
    delivered = [(row["thread_id"], json.loads(row["content"].split("Details:\n", 1)[1].split("\n\n")[0]))
                 for row in get_db().execute("SELECT thread_id, content FROM messages "
                                             "WHERE content LIKE '[EVENT: result]%'")]
    assert [thread for thread, _ in delivered] == [pr_owner]
    facts = delivered[0][1]
    assert facts["requester"] == OWNER and facts["item"] == f"{repo}#7" and facts["valid"] is True
