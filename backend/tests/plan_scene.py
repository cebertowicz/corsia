"""The plan scene the input-class cases and their siblings act on.

One plan, two node shapes, and the small readers every case needs: the shared
fixture lives here so the case table next door is only the table. `db` and the
decision helper come from `test_attention_queue`, which owns the schema-seeded
connection.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from plans import service
from plans.boundary import mutate
from tests.test_attention_queue import (  # noqa: F401 - shared plan fixtures
    _actor,
    _mid,
    db,
    raise_decision,
)

#: The plan owner thread from the shared fixture. Only a plan's owner runs its
#: tasks, so it is also the thread that reviews them.
ACTOR = _actor("root")

#: The six tables any refusal must leave untouched.
_DURABLE_TABLES = (
    "plan_nodes", "plan_node_events", "plan_node_dependencies",
    "plan_mutation_receipts", "continuation_triggers", "plan_liveness_wakes",
)

#: Placeholders resolved against the live scene, so each body below reads as
#: the body a caller would actually send.
NODE, OWN, REV = "<pending node>", "<own node>", "<current revision>"


@pytest.fixture
def scene(db):
    """One plan carrying the two node shapes the raise sites need.

    Both belong to `root`, the plan owner that runs and reviews them. `pending`
    is untouched; `own` is active, which is what the watch operations bind to.
    """
    plan = service.ensure_plan("thread", "root", db=db)
    pending = service.add_node(
        plan.id, kind="task", title="Pending", acceptance_criteria="Pending DOD", db=db,
    )
    own = service.add_node(
        plan.id, kind="task", title="Owned", acceptance_criteria="Owned DOD", db=db,
    )
    service.transition_node(own.id, "active", db=db)
    db.commit()
    return SimpleNamespace(plan_id=plan.id, pending=pending.id, own=own.id)


def _revision(db, node_id: str) -> int:
    return service.get_node(node_id, db=db).revision


def _snapshot(db) -> dict[str, list]:
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in _DURABLE_TABLES
    }


def _resolved(db, scene, params: dict) -> dict:
    """Substitute the scene's live ids and revision into one request body."""
    ids = {NODE: scene.pending, OWN: scene.own}
    body = {
        key: ids.get(value, value) if isinstance(value, str) else value
        for key, value in params.items()
    }
    if REV in [value for value in body.values() if isinstance(value, str)]:
        revision = _revision(db, body["node_id"])
        body = {
            key: revision if value == REV else value
            for key, value in body.items()
        }
    return body


def _mutate(db, plan_id, operation, params, mutation_id):
    return mutate(
        plan_id, operation, params, actor=ACTOR, provenance="test",
        mutation_id=mutation_id, db=db,
    )


def _operation(
    operation: str, params: dict, *, mutation_id: str | None = None, default_task_size: bool = True,
):
    """Build a case that sends one body through the real mutation boundary."""

    def build(db, scene):
        body = _resolved(db, scene, params)
        if default_task_size and operation in {
            "add", "insert", "split", "add_follow_up", "accept_with_follow_up",
        }:
            body.setdefault("size", "unknown")
        return partial(
            _mutate, db, scene.plan_id, operation, body,
            _mid() if mutation_id is None else mutation_id,
        )

    return build


def _remove_inherited_dependency(db, scene):
    phase = service.add_node(scene.plan_id, kind="phase", title="Phase", acceptance_criteria="Done", db=db)
    child = service.add_node(scene.plan_id, kind="task", title="Child", acceptance_criteria="Done", parent_id=phase.id, db=db)
    service.add_dependency(phase.id, scene.pending, db=db)
    return partial(_mutate, db, scene.plan_id, "remove_dependency", {
        "node_id": child.id, "depends_on_node_id": scene.pending, "expected_revision": child.revision,
    }, _mid())


def _artifact(db, *, external_id: str, owner: str | None) -> str:
    """Register one artifact identity, owned by a thread or by nobody.

    Returns the artifact's own id, which is what a wait binds to.
    """
    from events.identity import GITHUB_INTEGRATION, PR_TYPE
    from registry import artifact_record

    artifact_record.register_or_resolve(
        {
            "integration": GITHUB_INTEGRATION,
            "type": PR_TYPE,
            "external_id": external_id,
        },
        owner, db=db,
    )
    db.commit()
    return str(artifact_record.identity_row_for(external_id, db=db)["id"])
