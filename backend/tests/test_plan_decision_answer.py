"""Red-first P5 contract tests for decision answers, ingress, and projection.

Covers brief group D (answer atomicity), the production agent/tool and HTTP
ingress, and the operator projection/ETag contract for decisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor
from tests.test_plan_decisions import (  # noqa: F401 - shared red-phase fixtures
    OPTIONS,
    _DbProxy,
    _draft_kwargs,
    _immediate,
    _mid,
    _seed,
    _task,
    db,
)


def _thread_actor(thread_id: str = "worker") -> PlanActor:
    return PlanActor(thread_id, "user-1", "project-1")


def _human(user_id: str = "user-1") -> PlanActor:
    return PlanActor(None, user_id, None, kind="user")


def _raised_decision(db, *, title: str = "Where should decision state live?"):
    """Raise one decision that 'owner' authors in its own plan.

    Only a plan's owner authors its decisions. 'owner' is a root thread with no
    parent tier, so it also reviews and raises the decision itself.
    """
    from plans import decisions

    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Implement decisions", db=db)
    db.commit()
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="tool.plans.decision", db=db,
        **_draft_kwargs(title=title),
    ).result["decision"]
    decision = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="tool.plans.decision", db=db,
    ).result["decision"]
    decision = decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="tool.plans.decision", db=db,
    ).result["decision"]
    db.commit()
    return plan, source, decision


def _attention_state(db, decision_id: str):
    row = db.execute(
        """SELECT metadata FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision_id,),
    ).fetchone()
    meta = json.loads(row["metadata"]) if row["metadata"] else {}
    return meta.get("attention_state")


async def _await_detached_drives() -> None:
    """Wait for every post-response drive this loop started, never by sleeping."""
    from ws.ingress import background_tasks

    here = asyncio.get_running_loop()
    while pending := [
        task for task in background_tasks
        if task.get_loop() is here and not task.done()
    ]:
        await asyncio.gather(*pending, return_exceptions=True)


class _DrainingClient(TestClient):
    """A client whose request returns only after the detached drives finish.

    The route hands the post-commit drive to a tracked task so the response does
    not wait on the woken thread's Turn preparation. A test that asserts what
    the drive did awaits it explicitly here, inside the request's own loop.
    """

    @contextlib.contextmanager
    def _portal_factory(self):
        with super()._portal_factory() as portal:
            yield portal
            portal.call(_await_detached_drives)


def _client(
    db, monkeypatch, user_id: str = "user-1", *, token_kind: str = "session", drain: bool = True,
):
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = (
        lambda: {"id": user_id, "token_kind": token_kind}
    )
    return (_DrainingClient if drain else TestClient)(app)


# --- D. Answer atomicity ----------------------------------------------------


def test_an_option_answer_settles_state_attention_and_the_review_node_together(db):
    from plans import decisions

    plan, _source, decision = _raised_decision(db)

    answered = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    ).result["decision"]

    assert answered["state"] == "answered"
    assert answered["answer"] == {"option_id": "A", "text": None}
    assert _attention_state(db, decision["id"]) == "cleared"
    node = service._node_row(db, decision["node_id"])
    assert node["state"] == "accepted"
    event = db.execute(
        """SELECT * FROM plan_node_events
           WHERE node_id=? AND event='decision_answered'""",
        (decision["node_id"],),
    ).fetchone()
    assert event is not None
    assert json.loads(event["payload_json"])["option_id"] == "A"
    assert event["actor_id"] == "user:user-1"


def test_an_other_answer_records_bounded_free_text(db):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)

    answered = decisions.answer(
        decision["id"], option_id="other", other_text="  Do neither; split it.  ",
        expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_human(), provenance="p", db=db,
    ).result["decision"]

    assert answered["answer"] == {"option_id": "other", "text": "Do neither; split it."}
    assert _attention_state(db, decision["id"]) == "cleared"


@pytest.mark.parametrize(
    ("option_id", "other_text"),
    [
        ("C", None),
        ("", None),
        (None, None),
        ("other", None),
        ("other", "   "),
        ("other", "x" * 2001),
        ("A", "text alongside an option"),
    ],
)
def test_invalid_answers_are_rejected_and_leave_the_decision_raised(
    db, option_id, other_text,
):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)

    with pytest.raises(service.PlanError):
        decisions.answer(
            decision["id"], option_id=option_id, other_text=other_text,
            expected_revision=decision["revision"], mutation_id=_mid(),
            actor=_human(), provenance="p", db=db,
        )

    assert decisions.read(decision["id"], db=db)["state"] == "raised"
    assert _attention_state(db, decision["id"]) == "raised"
    assert service._node_row(db, decision["node_id"])["state"] == "review_requested"


def test_a_stale_expected_revision_is_rejected_without_settling_anything(db):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)

    with pytest.raises(service.PlanError):
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"] + 1,
            mutation_id=_mid(), actor=_human(), provenance="p", db=db,
        )

    assert _attention_state(db, decision["id"]) == "raised"
    assert service._node_row(db, decision["node_id"])["state"] == "review_requested"


def test_only_a_raised_decision_can_be_answered(db):
    from plans import decisions

    plan = service.ensure_plan("thread", "worker", db=db)
    source = _task(plan.id, "Implement", db=db)
    db.commit()
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db, **_draft_kwargs(),
    ).result["decision"]

    with pytest.raises(service.PlanError):
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_human(), provenance="p", db=db,
        )


def test_a_foreign_user_cannot_answer_and_learns_nothing(db):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)

    with pytest.raises((PermissionError, service.PlanError)) as excinfo:
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_human("user-2"), provenance="p", db=db,
        )

    assert decision["id"] not in str(excinfo.value)
    assert _attention_state(db, decision["id"]) == "raised"


def test_an_agent_thread_cannot_answer_on_the_users_behalf(db):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)

    with pytest.raises(PermissionError):
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
        )


@pytest.mark.parametrize(
    "boundary",
    ["attention_clear", "node_settlement", "event_record", "receipt_record"],
)
def test_a_failure_at_any_answer_boundary_restores_all_pre_answer_state(
    db, monkeypatch, boundary,
):
    from plans import decisions, receipts
    from registry import attention as registry_attention

    _plan, _source, decision = _raised_decision(db)
    before = decisions.read(decision["id"], db=db)
    plan_revision_before = db.execute(
        "SELECT revision FROM plans WHERE id=?", (decision["plan_id"],)
    ).fetchone()["revision"]

    def explode(*_args, **_kwargs):
        raise RuntimeError(f"injected {boundary} failure")

    targets = {
        "attention_clear": (registry_attention, "clear_attention"),
        "node_settlement": (service, "_transition"),
        "event_record": (service, "_event"),
        "receipt_record": (receipts, "record"),
    }
    module, name = targets[boundary]
    monkeypatch.setattr(module, name, explode)

    with pytest.raises(RuntimeError):
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_human(), provenance="p", db=db,
        )

    monkeypatch.undo()
    assert decisions.read(decision["id"], db=db) == before
    assert _attention_state(db, decision["id"]) == "raised"
    assert service._node_row(db, decision["node_id"])["state"] == "review_requested"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='decision_answered'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT revision FROM plans WHERE id=?", (decision["plan_id"],)
    ).fetchone()["revision"] == plan_revision_before


def test_answer_replay_returns_the_same_result_and_nudges_once(db):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)
    mutation_id = _mid()

    first = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_human(), provenance="p", db=db,
    )
    second = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_human(), provenance="p", db=db,
    )

    assert first.result == second.result
    assert len(first.messages) == 1
    assert second.messages == ()
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='decision_answered'"
    ).fetchone()[0] == 1


def test_a_broadcast_failure_after_commit_never_reports_the_answer_as_failed(
    db, monkeypatch,
):
    from infra import state
    from plans import decisions, notifications

    _plan, _source, decision = _raised_decision(db)

    def explode(*_args, **_kwargs):
        raise RuntimeError("socket is gone")

    deferred = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="p", db=db,
    )
    monkeypatch.setattr(state, "broadcast_to_user", explode)

    result = notifications.deliver_deferred(deferred)

    assert result["decision"]["state"] == "answered"
    assert _attention_state(db, decision["id"]) == "cleared"


def test_concurrent_answers_produce_exactly_one_winner(tmp_path):
    from plans import decisions

    path = tmp_path / "answer.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    setup = connect()
    setup.executescript(SCHEMA_SQL)
    _seed(setup)
    _plan, _source, decision = _raised_decision(setup)
    setup.commit()
    setup.close()

    start = threading.Barrier(2)
    wins: list[dict] = []
    losses: list[BaseException] = []

    def run(index: int) -> None:
        connection = connect()
        try:
            start.wait(timeout=10)
            wins.append(decisions.answer(
                decision["id"], option_id="A" if index == 0 else "B",
                expected_revision=decision["revision"],
                mutation_id=f"answer-{index}", actor=_human(), provenance="p",
                db=connection,
            ).result["decision"])
        except (service.PlanError, PermissionError) as exc:
            losses.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(wins) == 1
    assert len(losses) == 1
    verify = connect()
    try:
        assert verify.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE event='decision_answered'"
        ).fetchone()[0] == 1
        assert service._node_row(verify, decision["node_id"])["state"] == "accepted"
    finally:
        verify.close()


def test_answering_does_not_touch_the_source_work_node(db):
    from plans import decisions

    _plan, source, decision = _raised_decision(db)
    before = service._node_row(db, source.id)["state"]

    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="p", db=db,
    )

    assert service._node_row(db, source.id)["state"] == before


# --- Operator projection and freshness --------------------------------------


# Queue membership, cross-source ordering, and the plan's blocking indicator
# now live in `test_attention_queue.py` and `test_plan_blocking_indicator.py`:
# the operator envelope no longer publishes decision bodies at all.


def test_the_revision_token_changes_when_decision_order_or_state_changes(db):
    from plans import decisions, operator_projection

    _plan, _source, decision = _raised_decision(db)
    before = operator_projection.read_thread(
        "owner", actor=_human(), db=db,
    )["revision_token"]

    decisions.reorder(
        decision["id"], order_key=7, expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
    )
    db.commit()
    after_reorder = operator_projection.read_thread(
        "owner", actor=_human(), db=db,
    )["revision_token"]

    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"] + 1,
        mutation_id=_mid(), actor=_human(), provenance="p", db=db,
    )
    db.commit()
    after_answer = operator_projection.read_thread(
        "owner", actor=_human(), db=db,
    )["revision_token"]

    assert len({before, after_reorder, after_answer}) == 3


# --- Production ingress ------------------------------------------------------


def test_the_http_answer_route_is_authenticated_idempotent_and_non_disclosing(
    db, monkeypatch,
):
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch)
    body = {"option_id": "A", "expected_revision": decision["revision"],
            "mutation_id": "http-answer-1"}

    first = client.post(f"/api/plans/decisions/{decision['id']}/answer", json=body)
    second = client.post(f"/api/plans/decisions/{decision['id']}/answer", json=body)

    assert first.status_code == 200
    assert first.json()["decision"]["state"] == "answered"
    assert second.status_code == 200
    assert second.json() == first.json()
    assert decisions.read(decision["id"], db=db)["state"] == "answered"


def test_the_http_answer_route_rejects_an_api_token(db, monkeypatch):
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch, token_kind="api")

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"],
              "mutation_id": "http-answer-api-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "This change needs the signed-in person: open CORSIA and make it there."
    )
    assert _attention_state(db, decision["id"]) == "raised"


def test_the_http_answer_route_denies_a_foreign_user_without_leaking_ids(
    db, monkeypatch,
):
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch, user_id="user-2")

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"],
              "mutation_id": "http-answer-2"},
    )

    assert response.status_code in (403, 404)
    assert decision["id"] not in response.text
    assert _attention_state(db, decision["id"]) == "raised"


def test_the_http_answer_route_rejects_a_missing_mutation_id(db, monkeypatch):
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"]},
    )

    assert response.status_code == 422


def test_the_http_answer_route_reports_a_stale_revision_as_a_conflict(db, monkeypatch):
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"] + 5,
              "mutation_id": "http-answer-3"},
    )

    assert response.status_code == 409
    assert _attention_state(db, decision["id"]) == "raised"


def test_the_http_answer_route_never_accepts_a_caller_supplied_actor(db, monkeypatch):
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"],
              "mutation_id": "http-answer-4", "actor_session_id": "owner"},
    )

    assert response.status_code == 422



@pytest.mark.asyncio
async def test_the_agent_tool_refuses_an_out_of_scope_plan_without_disclosure(
    db, monkeypatch,
):
    from tools import plans as tool_plans

    plan = service.ensure_plan("thread", "foreign", db=db)
    source = _task(plan.id, "Foreign", db=db)
    db.commit()

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        tool_plans, "_require_context",
        lambda kwargs: ("worker", {"user_id": "user-1", "project_id": "project-1"}),
    )

    result = await tool_plans.PlanDecisionTool().execute(
        plan_id=plan.id, mutation_id="tool-2", operation="draft",
        params={"source_node_id": source.id, **_draft_kwargs()},
    )

    assert result.startswith("Error:")
    assert plan.id not in result
    assert source.id not in result


def test_the_decision_tool_is_registered_on_the_production_agent_and_catalog():
    """The domain service must have a real caller, not only tests."""
    import inspect

    from core import agent as core_agent
    from tools import catalog

    source = inspect.getsource(core_agent)
    assert "PlanDecisionTool" in source
    assert "agent.tools.register(PlanDecisionTool())" in source
    entry = catalog.TOOL_CATALOG["plan_decision"]
    assert entry.category == "plans"


@pytest.mark.asyncio
async def test_the_agent_tool_drives_the_whole_author_lifecycle(db, monkeypatch):
    """Every author operation must be reachable from the production tool.

    The delegated child 'worker' authors in its own plan; 'owner', the parent
    that spawned it, reviews and raises through the same tool.
    """
    from plans import decisions
    from tools import plans as tool_plans

    plan = service.ensure_plan("thread", "worker", db=db)
    source = _task(plan.id, "Implement", db=db)
    db.commit()

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    actors = {"worker": ("worker", {"user_id": "user-1", "project_id": "project-1"}),
              "owner": ("owner", {"user_id": "user-1", "project_id": "project-1"})}
    current = {"as": "worker"}
    monkeypatch.setattr(
        tool_plans, "_require_context", lambda kwargs: actors[current["as"]],
    )
    tool = tool_plans.PlanDecisionTool()

    async def call(operation: str, params: dict, mutation_id: str) -> dict:
        raw = await tool.execute(
            plan_id=plan.id, mutation_id=mutation_id, operation=operation,
            params=params,
        )
        assert not raw.startswith("Error:"), raw
        return json.loads(raw)["decision"]

    drafted = await call("draft", {"source_node_id": source.id, **_draft_kwargs()}, "t1")
    updated = await call(
        "update",
        {"decision_id": drafted["id"], "expected_revision": drafted["revision"],
         "context": "Revised context after the parent's note."},
        "t2",
    )
    reordered = await call(
        "reorder",
        {"decision_id": updated["id"], "expected_revision": updated["revision"],
         "order_key": 4},
        "t3",
    )
    current["as"] = "owner"
    reviewed = await call(
        "parent_review",
        {"decision_id": reordered["id"], "expected_revision": reordered["revision"]},
        "t4",
    )
    raised = await call(
        "raise",
        {"decision_id": reviewed["id"], "expected_revision": reviewed["revision"]},
        "t5",
    )
    invalidated = await call(
        "invalidate",
        {"decision_id": raised["id"], "expected_revision": raised["revision"]},
        "t6",
    )

    assert updated["context"] == "Revised context after the parent's note."
    assert reordered["order_key"] == 4
    assert reviewed["state"] == "parent_reviewed"
    assert raised["state"] == "raised"
    assert invalidated["state"] == "invalidated"
    assert decisions.read(drafted["id"], db=db)["state"] == "invalidated"
    assert _attention_state(db, drafted["id"]) == "cleared"


@pytest.mark.asyncio
async def test_the_agent_tool_refuses_the_users_answer_operation(db, monkeypatch):
    """Answering is a human action; the agent surface must not expose it."""
    from tools import plans as tool_plans

    _plan, _source, decision = _raised_decision(db)
    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        tool_plans, "_require_context",
        lambda kwargs: ("owner", {"user_id": "user-1", "project_id": "project-1"}),
    )
    tool = tool_plans.PlanDecisionTool()
    assert "answer" not in tool.parameters["properties"]["operation"]["enum"]

    result = await tool.execute(
        plan_id=decision["plan_id"], mutation_id="tool-answer", operation="answer",
        params={"decision_id": decision["id"], "option_id": "A",
                "expected_revision": decision["revision"]},
    )

    assert result.startswith("Error:")
    assert _attention_state(db, decision["id"]) == "raised"
