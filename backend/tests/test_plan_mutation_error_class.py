"""One error taxonomy for plan mutations, proven over every operation.

Two refusal classes, distinguished by what the caller must do next. A malformed
body is permanently malformed — re-reading changes nothing, so it is 422. A
target that moved is 409, because re-reading is exactly the remedy.

Head returned 409 for both. A conforming client told to "re-read and retry with
fresh evidence" over a body it could have fixed retries forever, which is why
this is pinned across *every* published operation rather than a sample, and
across both caller surfaces rather than just HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator

import itertools
import sqlite3

import pytest
from fastapi import HTTPException

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate

_MUTATION_IDS = itertools.count(1)

#: The six tables any refusal must leave untouched.
_DURABLE_TABLES = (
    "plan_nodes", "plan_node_events", "plan_node_dependencies",
    "plan_mutation_receipts", "continuation_triggers", "plan_liveness_wakes",
)


class _DbProxy:
    """Keep the fixture connection open when route code closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
            "VALUES (?,'user-1',?,'t','t')",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def plan_and_node(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Implement",
        acceptance_criteria="Tests pass", db=db,
    )
    db.commit()
    return plan, node


def _snapshot(db) -> dict[str, list]:
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in _DURABLE_TABLES
    }


async def _over_http(db, monkeypatch, plan_id, body):
    from api import plans as api_plans

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", immediate)
    return await api_plans.mutate_plan(plan_id, body, {"id": "user-1"})


def _body(operation, params):
    return {
        "operation": operation,
        "mutation_id": f"class-{next(_MUTATION_IDS)}",
        "params": params,
    }


#: Every operation `tools/plans.py` publishes, with one body that is malformed
#: for that operation specifically. A field from another operation is the
#: shape error every operation shares, so it is the portable probe; operations
#: whose own required field can be omitted use that instead.
_INVALID_BODIES = {
    "add": {"kind": "task", "title": "T", "acceptance_criteria": "D",
            "expected_revision": 1},
    "insert": {"kind": "task", "title": "T", "acceptance_criteria": "D",
               "expected_revision": 1},
    "edit": {"node_id": "NODE"},
    "split": {"node_id": "NODE", "expected_revision": 0},
    "reorder": {"node_id": "NODE", "expected_revision": 0},
    "activate": {"node_id": "NODE"},
    "start": {"node_id": "NODE"},
    "pause_node": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "resume_node": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "block_node": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "resolve_blocker": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "request_review": {"node_id": "NODE"},
    "mark_done": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    # The three review operations authorize the reviewer before reaching their
    # own field validation, and that precedence is deliberate. Their probe is a
    # cross-operation field, which `validate_operation_params` rejects before
    # any authorization runs — still a caller-fixable input error.
    "reject": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "fail": {"node_id": "NODE"},
    "attach_spawned_thread": {"node_id": "NODE", "expected_revision": 0},
    "replace_spawned_thread": {"node_id": "NODE", "expected_revision": 0},
    "accept": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "accept_with_follow_up": {"node_id": "NODE", "expected_revision": 0,
                              "order_key": 1},
    "arm_watch": {"node_id": "NODE"},
    # Watch operations bind to a thread actor before reading their own fields,
    # so a human caller is refused on authorization first; the portable probe
    # is again a cross-operation field.
    "resolve_watch": {"node_id": "NODE", "expected_revision": 0, "order_key": 1},
    "add_dependency": {"node_id": "NODE", "expected_revision": 0},
    "remove_dependency": {"node_id": "NODE", "expected_revision": 0},
    "add_follow_up": {"node_id": "NODE", "expected_revision": 0},
    "add_correction": {"node_id": "NODE", "expected_revision": 0},
    "archive_node": {"node_id": "NODE", "order_key": 3, "expected_revision": 0},
    "close_node": {"node_id": "NODE", "order_key": 3, "expected_revision": 0},
    "pause": {"node_id": "NODE", "expected_revision": 0},
}


def test_the_matrix_covers_every_published_operation():
    """A sample is not a matrix: an operation added without a row here would
    ship unclassified."""
    from tools import plans as tool_plans

    # The user's pause is not published to agents but still reaches the
    # boundary over HTTP, so its malformed bodies stay classified too.
    assert set(_INVALID_BODIES) == set(tool_plans._OPERATIONS) | {"pause", "pause_node"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(_INVALID_BODIES))
async def test_every_operation_reports_a_malformed_body_as_422(
    db, plan_and_node, monkeypatch, operation,
):
    plan, node = plan_and_node
    params = {
        key: (node.id if value == "NODE" else value)
        for key, value in _INVALID_BODIES[operation].items()
    }
    before = _snapshot(db)

    with pytest.raises(HTTPException) as refusal:
        await _over_http(db, monkeypatch, plan.id, _body(operation, params))

    assert refusal.value.status_code == 422
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_an_unsupported_operation_is_422(db, plan_and_node, monkeypatch):
    plan, node = plan_and_node

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, plan.id,
            _body("teleport", {"node_id": node.id, "expected_revision": 1}),
        )

    assert refusal.value.status_code == 422


# --- conflicts must stay 409 ------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_revision_stays_409(db, plan_and_node, monkeypatch):
    plan, node = plan_and_node
    service.transition_node(node.id, "active", db=db)

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, plan.id,
            _body(
                "request_review",
                {
                    "node_id": node.id,
                    "evidence": "Focused proof passed",
                    "expected_revision": node.revision,
                },
            ),
        )

    assert refusal.value.status_code == 409
    assert "revision conflict" in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_an_illegal_transition_stays_409(db, plan_and_node, monkeypatch):
    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, plan.id,
            _body(
                "request_review",
                {
                    "node_id": node.id,
                    "evidence": "Focused proof passed",
                    "expected_revision": current.revision,
                },
            ),
        )

    assert refusal.value.status_code == 409
    assert "requires active or review-requested work" in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_an_order_key_outside_the_sibling_range_stays_409(
    db, plan_and_node, monkeypatch,
):
    """The range is a fact about current siblings, so re-reading tells the
    caller what it is. That makes it a conflict, not a shape error."""
    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, plan.id,
            _body("reorder", {"node_id": node.id, "order_key": 99,
                              "expected_revision": current.revision}),
        )

    assert refusal.value.status_code == 409
    assert "outside the sibling range" in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_a_reused_mutation_id_with_a_different_body_stays_409(
    db, plan_and_node, monkeypatch,
):
    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)
    reused = "class-replay"
    await _over_http(
        db, monkeypatch, plan.id,
        {"operation": "activate", "mutation_id": reused,
         "params": {"node_id": node.id, "expected_revision": current.revision}},
    )

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, plan.id,
            {"operation": "activate", "mutation_id": reused,
             "params": {"node_id": node.id, "expected_revision": 99}},
        )

    assert refusal.value.status_code == 409


# --- authorization still comes first ---------------------------------------


@pytest.mark.asyncio
async def test_a_forbidden_plan_stays_403_not_422(db, monkeypatch):
    """Scope authorization is not weakened by the new class: a well-formed body
    against a plan the caller cannot reach is still 403."""
    other = service.ensure_plan("thread", "worker", db=db)
    db.execute("UPDATE threads SET user_id='user-2' WHERE id='worker'")
    db.commit()

    with pytest.raises(HTTPException) as refusal:
        await _over_http(
            db, monkeypatch, other.id,
            _body("activate", {"node_id": "nope", "expected_revision": 1}),
        )

    assert refusal.value.status_code == 403


@pytest.mark.parametrize("operation", ["accept", "reject"])
def test_a_non_owner_review_is_refused_before_its_body_is_validated(
    db, plan_and_node, operation,
):
    """Reviewer authorization precedes the operation's own field validation, and
    this correction does not reorder that. A thread that is not the plan owner,
    sending a body that is also incomplete, learns it may not review, not what
    the body should contain. Every human passes the reviewer check through
    fallback, so only a thread caller can be refused here; both surfaces map
    `PermissionError` to their forbidden class."""
    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)
    before = _snapshot(db)

    with pytest.raises(PermissionError, match="plan owner or human fallback"):
        mutate(
            plan.id, operation,
            {"node_id": node.id, "expected_revision": current.revision},
            actor=PlanActor("worker", "user-1", None), provenance="test",
            mutation_id=f"class-{next(_MUTATION_IDS)}", db=db,
        )

    assert _snapshot(db) == before


# --- both caller surfaces agree --------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_surface_reports_the_input_class_distinctly(
    db, plan_and_node, monkeypatch,
):
    """The tool caller must be able to tell the two apart too, or the fork this
    taxonomy removes simply reappears one layer up."""
    from tools import plans as tool_plans

    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", immediate)
    monkeypatch.setattr(
        tool_plans, "_require_context",
        lambda kwargs: ("parent", {"user_id": "user-1", "project_id": None}),
    )
    tool = tool_plans.PlanUpdateTool()

    malformed = await tool.execute(
        plan_id=plan.id, mutation_id="tool-shape", operation="activate",
        params={"node_id": node.id},
    )
    conflict = await tool.execute(
        plan_id=plan.id, mutation_id="tool-conflict", operation="request_review",
        params={
            "node_id": node.id,
            "evidence": "Focused proof passed",
            "expected_revision": current.revision,
        },
    )

    assert "invalid request" in malformed.casefold()
    assert "invalid request" not in conflict.casefold()
    assert "state precondition" in conflict.casefold()
    assert "change the request" in conflict.casefold()

    stale = await tool.execute(
        plan_id=plan.id, mutation_id="tool-stale", operation="edit",
        params={
            "node_id": node.id, "title": "Renamed",
            "expected_revision": current.revision + 7,
        },
    )

    # Only a stale revision is classed by its type, never by message text.
    assert "re-read the plan and retry" in stale
    assert "revision conflict" in stale
    assert "state precondition" not in stale


def test_the_input_class_is_a_plan_error_subclass():
    """Rollback stays a clean revert: every `except PlanError` site is
    unchanged, so only callers that opt in see the distinction."""
    from plans import errors

    assert issubclass(errors.PlanInputError, errors.PlanError)
    assert service.PlanError is errors.PlanError
    assert service.PlanInputError is errors.PlanInputError
