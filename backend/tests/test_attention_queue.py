"""Contract tests for the one user-global attention queue.

A decision is a first-class user-attention item, not plan payload. The queue the
user works through is the existing `registry.attention` pile, read user-globally
and annotated with the typed detail each renderer needs — so a decision raised by
a subagent two levels down is reachable without knowing which plan the user is
currently looking at.

Red at `19ea844a`: `plans.attention_detail` does not exist, and the candidate's
plan-scoped projection drops exactly these rows.
"""

from __future__ import annotations

from collections.abc import Iterator

import json
import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
import plans.decisions as decisions
import plans.service as service
from plans.boundary import PlanActor, mutate


def _seed(connection) -> None:
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project One','t','t')"
    )
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-2','Other','t','t')"
    )
    for thread_id, user_id, project_id, spawned_by, title in (
        ("root", "user-1", "project-1", None, "Parent work"),
        ("child", "user-1", "project-1", "root", "Deep subagent"),
        ("grandchild", "user-1", "project-1", "child", "Deeper subagent"),
        ("other-root", "user-1", "project-1", None, "Another thread"),
        ("foreign", "user-2", "project-2", None, "Foreign thread"),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,lifecycle_state,archived_at,
                title,model,created_at,updated_at)
               VALUES (?,?,?,?, 'active', NULL, ?,'claude-code/sonnet','t','t')""",
            (thread_id, user_id, project_id, spawned_by, title),
        )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


#: Every seeded thread except `foreign` belongs to user-1/project-1.
_SCOPES = {"foreign": ("user-2", "project-2")}


def _actor(thread_id: str):
    user_id, project = _SCOPES.get(thread_id, ("user-1", "project-1"))
    return PlanActor(thread_id, user_id, project)


def _mid(counter=[0]) -> str:  # noqa: B006 - deliberate per-process sequence
    counter[0] += 1
    return f"queue-mutation-{counter[0]}"


def _body(**overrides) -> dict:
    payload = {
        "title": "Where should decision state live?",
        "context": "P5 needs one canonical home for decision content.",
        "tradeoff": "A table is typed; the identity avoids a second row.",
        "recommendation": "B",
        "rationale": "CORSIA owns the identity, so no twin is required.",
        "options": [
            {"id": "A", "label": "Ship the twin", "detail": "Adds one table."},
            {"id": "B", "label": "Reuse the identity", "detail": "No schema change."},
        ],
        "other_enabled": True,
    }
    payload.update(overrides)
    return payload


def raise_decision(db, owner_thread: str, author: str, **overrides) -> dict:
    """Draft in `owner_thread`'s plan as `author`, parent-review, and raise.

    Only a plan's owner authors its decisions, so `author` must be the plan
    owner; `decision_authz` refuses any other thread. The review tier is the
    thread that spawned the owner, or the owner itself for root work.
    """
    plan = service.ensure_plan("thread", owner_thread, db=db)
    node = service.add_node(
        plan.id, kind="task", title=f"Work in {author}",
        acceptance_criteria="Work DOD", db=db,
    )
    db.commit()
    decision = decisions.draft(
        plan.id, source_node_id=node.id, mutation_id=_mid(),
        actor=_actor(author), provenance="tool.plans.decision", db=db,
        **_body(**overrides),
    ).result["decision"]
    reviewer = db.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (owner_thread,),
    ).fetchone()["spawned_by"]
    reviewer_actor = _actor(reviewer or owner_thread)
    decision = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=reviewer_actor, provenance="p", db=db,
    ).result["decision"]
    decision = decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=reviewer_actor, provenance="p", db=db,
    ).result["decision"]
    db.commit()
    return decision


def test_a_raised_decision_holds_its_source_without_a_second_user_block(db):
    from registry import models

    decision = raise_decision(db, "root", "root")
    models.register_artifact(
        integration="workspace", type="document", external_id="decision-context.md",
        parent_thread_id="root", title="Decision context", db=db,
    )
    ask = db.execute(
        "SELECT id FROM artifact_identities WHERE integration='workspace' "
        "AND type='document' AND external_id='decision-context.md'",
    ).fetchone()["id"]

    with pytest.raises(service.PlanInputError, match="already holds it"):
        mutate(
            decision["plan_id"], "block_node", {
                "node_id": decision["source_node_id"],
                "reason_key": "decision", "reason": "Answer the question",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": ask,
                "expected_revision": service._node_row(
                    db, decision["source_node_id"],
                )["revision"],
            }, actor=_actor("root"), provenance="test.attention",
            mutation_id=_mid(), db=db,
        )


def _raised_items(db, user_id: str = "user-1") -> list[dict]:
    """The authorized raised rows, exactly as the HTTP surface resolves them."""
    from infra.auth import may_access_artifact
    from registry import attention

    return [
        item for item in attention.list_raised(db=db)
        if item.get("owner_thread") is not None
        and may_access_artifact(item["owner_thread"], user_id, db=db)
    ]


def queue_for(db, user_id: str = "user-1") -> list[dict]:
    import plans.attention_detail as attention_detail

    return attention_detail.queue(
        attention_detail.annotate(_raised_items(db, user_id), db=db)
    )


def generic_for(db, user_id: str = "user-1") -> list[dict]:
    import plans.attention_detail as attention_detail

    return attention_detail.generic(
        attention_detail.annotate(_raised_items(db, user_id), db=db)
    )


# --- D3: the subagent decision the plan-scoped projection drops ---------------


def test_a_decision_raised_by_a_subagent_is_in_the_users_queue(db):
    decision = raise_decision(db, "child", "child")

    queued = queue_for(db)

    assert [row["external_id"] for row in queued] == [decision["id"]]
    assert queued[0]["renderer"] == "decision"
    assert queued[0]["detail"]["title"] == "Where should decision state live?"


def test_a_decision_two_levels_down_is_in_the_users_queue(db):
    decision = raise_decision(db, "grandchild", "grandchild")

    assert [row["external_id"] for row in queue_for(db)] == [decision["id"]]


def test_the_queue_names_no_plan_target_at_all(db):
    """The whole point: reachability must not depend on the current plan view."""
    raise_decision(db, "child", "child")
    raise_decision(db, "other-root", "other-root")

    assert len(queue_for(db)) == 2


def test_the_plan_indicator_and_the_user_queue_agree(db):
    """The queue is user-global; the plan says only that it awaits an answer.

    They read different things, so the invariant that matters is that they can
    never disagree about whether a question is still live.
    """
    from plans import operator_projection

    raise_decision(db, "child", "child")
    human = PlanActor(None, "user-1", None, kind="user")

    thread = operator_projection.read_thread("child", actor=human, db=db)

    assert thread["attention"]["awaiting_count"] == 1
    assert len(queue_for(db)) == 1


# --- attribution and provenance ----------------------------------------


def test_detail_attributes_the_decision_to_its_real_source_thread(db):
    raise_decision(db, "child", "child")

    source = queue_for(db)[0]["detail"]["source"]

    assert source["thread_id"] == "child"
    assert source["thread_label"] == "Deep subagent"
    assert source["project_label"] == "Project One"


def test_root_direct_provenance_is_labelled_truthfully(db):
    """A root thread has no parent tier, so its raise was never parent-reviewed."""
    raise_decision(db, "root", "root")

    assert queue_for(db)[0]["detail"]["source"]["parent_reviewed"] is False


def test_delegated_work_reports_a_real_parent_review(db):
    """A child's decision in its own plan is reviewed by the parent that spawned it."""
    raise_decision(db, "child", "child")

    assert queue_for(db)[0]["detail"]["source"]["parent_reviewed"] is True


# --- the safety property the candidate lacked --------------------------------


def test_a_row_no_renderer_can_draw_stays_in_the_generic_list(db):
    from registry import attention, models

    from infra.database import write_transaction
    with write_transaction(db, scope="test_attention_setup"):
        models.register_artifact(
            integration="local", type="note", external_id="ordinary-1",
            parent_thread_id="root", title="Ordinary", db=db,
        )
        ordinary = db.execute(
            "SELECT id FROM artifact_identities WHERE external_id='ordinary-1'"
        ).fetchone()["id"]
        attention.raise_attention(ordinary, db=db)
    db.commit()

    assert [row["external_id"] for row in generic_for(db)] == ["ordinary-1"]
    assert queue_for(db) == []


def test_a_decision_whose_body_cannot_be_read_stays_generic_rather_than_vanishing(db):
    """Hide only what is actually drawn — never subtract an undrawable row."""
    decision = raise_decision(db, "child", "child")
    db.execute(
        "UPDATE artifact_identities SET metadata=? WHERE external_id=?",
        (json.dumps({"attention_state": "raised", "plan_decision": {"broken": True}}),
         decision["id"]),
    )
    db.commit()

    assert queue_for(db) == []
    assert [row["external_id"] for row in generic_for(db)] == [decision["id"]]


def test_an_answered_decision_leaves_the_queue(db):
    decision = raise_decision(db, "child", "child")
    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=PlanActor(None, "user-1", None, kind="user"),
        provenance="p", db=db,
    )
    db.commit()

    assert queue_for(db) == []


# --- canonical order ----------------------------------------------------------


def test_agent_priority_beats_age_across_sources(db):
    """Both are `order_key` 0 on their own plans, so age decides — until an
    agent explicitly deprioritizes its own question."""
    child = raise_decision(db, "child", "child", title="Child question")
    raise_decision(db, "other-root", "other-root", title="Root question")
    assert [row["detail"]["title"] for row in queue_for(db)] == [
        "Child question", "Root question",
    ]

    decisions.reorder(
        child["id"], order_key=5, expected_revision=child["revision"],
        mutation_id=_mid(), actor=_actor("child"), provenance="p", db=db,
    )
    db.commit()

    assert [row["detail"]["title"] for row in queue_for(db)] == [
        "Root question", "Child question",
    ]


def test_the_queue_order_is_deterministic_and_carries_no_duplicates(db):
    ids = [raise_decision(db, "child", "child", title=f"Q{index}")["id"]
           for index in range(3)]

    first = [row["external_id"] for row in queue_for(db)]
    second = [row["external_id"] for row in queue_for(db)]

    assert first == second
    assert sorted(first) == sorted(ids)


# --- privacy ------------------------------------------------------------------


def test_another_users_decision_never_enters_this_users_queue(db):
    raise_decision(db, "foreign", "foreign")

    assert queue_for(db, "user-1") == []
    assert generic_for(db, "user-1") == []


# --- cost ---------------------------------------------------------------------


def test_annotation_is_bounded_by_the_raised_batch_not_the_lifetime_total(db):
    """A2 from the exact-head review: no unbounded global identity scan."""
    import plans.attention_detail as attention_detail

    for index in range(5):
        decision = raise_decision(db, "child", "child", title=f"Answered {index}")
        decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=PlanActor(None, "user-1", None, kind="user"),
            provenance="p", db=db,
        )
    live = raise_decision(db, "child", "child", title="Still open")
    db.commit()

    scanned: list[tuple] = []

    class Recording:
        """sqlite3.Connection.execute is read-only, so observe through a proxy."""

        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, parameters=()):
            if "artifact_identities" in sql and "IN (" in sql:
                scanned.append((sql, parameters))
            return self._connection.execute(sql, parameters)

    queued = attention_detail.annotate(_raised_items(db), db=Recording(db))

    assert [row["external_id"] for row in attention_detail.queue(queued)] == [live["id"]]
    # Every identity lookup is bounded by the raised batch (one row), never by
    # the six decisions that have ever existed.
    assert scanned
    for _sql, parameters in scanned:
        assert len(parameters) == 1
