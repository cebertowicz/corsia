"""Red-first P5 contract tests for structured plan decisions.

Covers brief groups A (structure/validation), B (lifecycle/authorization),
C (one attention pile), and E (invalidation/ETag). Answer atomicity (group D)
and production ingress live in ``test_plan_decision_answer.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import json
import sqlite3
import threading

import pytest

from infra.schema import SCHEMA_SQL
import plans.decisions as decisions
import plans.service as service
from plans.boundary import PlanActor


async def _immediate(callable_):
    return callable_()


class _DbProxy:
    """Keep the fixture connection open when an API operation closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


def _seed(connection) -> None:
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-2','Other','t','t')"
    )
    for thread_id, user_id, project_id, spawned_by, lifecycle, archived in (
        ("owner", "user-1", "project-1", None, "active", None),
        ("worker", "user-1", "project-1", "owner", "active", None),
        ("cousin", "user-1", "project-1", "owner", "active", None),
        ("archived", "user-1", "project-1", None, "archived", "t"),
        ("foreign", "user-2", "project-2", None, "active", None),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,lifecycle_state,archived_at,
                title,model,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'claude-code/sonnet','t','t')""",
            (thread_id, user_id, project_id, spawned_by, lifecycle, archived,
             f"{thread_id} thread"),
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


def _thread_actor(thread_id: str = "worker") -> PlanActor:
    return PlanActor(thread_id, "user-1", "project-1")


def _human(user_id: str = "user-1") -> PlanActor:
    return PlanActor(None, user_id, None, kind="user")


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )


OPTIONS = [
    {"id": "A", "label": "Ship the twin", "detail": "Adds one table."},
    {"id": "B", "label": "Reuse the identity", "detail": "No schema change."},
]


def _draft_kwargs(**overrides) -> dict:
    payload = {
        "title": "Where should decision state live?",
        "context": "P5 needs one canonical home for decision content.",
        "tradeoff": "A table is typed; the identity avoids a second row.",
        "recommendation": "B",
        "rationale": "CORSIA owns the identity, so no twin is required.",
        "options": [dict(option) for option in OPTIONS],
        "other_enabled": True,
    }
    payload.update(overrides)
    return payload


def _mid(counter=[0]) -> str:  # noqa: B006 - deliberate per-process sequence
    counter[0] += 1
    return f"mutation-{counter[0]}"


def _delegated(db):
    """The delegated child 'worker''s own plan and a work node in it.

    Only a plan's owner runs its tasks, so the child authors decisions in its own
    plan and 'owner', the thread that spawned it, is the parent review tier.
    """
    plan = service.ensure_plan("thread", "worker", db=db)
    source = _task(plan.id, "Implement decisions", db=db)
    db.commit()
    return plan, source


def _draft(db, plan, source, actor=None, **overrides):
    import plans.decisions as decisions

    return decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(),
        actor=actor or _thread_actor("worker"), provenance="tool.plans.decision",
        db=db, **_draft_kwargs(**overrides),
    ).result["decision"]


def _reviewed(db, plan, source, **overrides):
    import plans.decisions as decisions

    decision = _draft(db, plan, source, **overrides)
    return decisions.parent_review(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="tool.plans.decision", db=db,
    ).result["decision"]


def _raised(db, plan, source, **overrides):
    import plans.decisions as decisions

    decision = _reviewed(db, plan, source, **overrides)
    return decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="tool.plans.decision", db=db,
    ).result["decision"]


def _artifact(db, decision_id: str):
    return db.execute(
        """SELECT * FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision_id,),
    ).fetchone()


def _attention_state(db, decision_id: str):
    row = _artifact(db, decision_id)
    meta = json.loads(row["metadata"]) if row["metadata"] else {}
    return meta.get("attention_state")


# --- A. Structure and validation --------------------------------------------


def test_draft_persists_every_required_structured_field_on_one_identity(db):
    plan, source = _delegated(db)

    decision = _draft(db, plan, source)

    assert decision["plan_id"] == plan.id
    assert decision["source_node_id"] == source.id
    assert decision["thread_id"] == "worker"
    assert decision["state"] == "draft"
    assert decision["revision"] == 1
    assert decision["order_key"] == 0
    assert decision["other_enabled"] is True
    assert decision["recommendation"] == "B"
    assert [option["id"] for option in decision["options"]] == ["A", "B"]
    assert decision["context"].startswith("P5 needs one canonical home")
    assert decision["tradeoff"]
    assert decision["rationale"]

    row = _artifact(db, decision["id"])
    assert row is not None and row["state"] == "active"
    stored = json.loads(row["metadata"])["plan_decision"]
    assert stored["state"] == "draft"
    assert stored["source_node_id"] == source.id


def test_draft_creates_its_own_user_assigned_review_node_linked_to_the_identity(db):
    plan, source = _delegated(db)

    decision = _draft(db, plan, source)

    node = service._node_row(db, decision["node_id"])
    assert node["plan_id"] == plan.id
    assert node["assignee"] == "user"
    assert node["state"] == "pending"
    assert node["owner_thread_id"] is None
    assert node["id"] != source.id
    link = db.execute(
        """SELECT * FROM plan_node_artifact_links
           WHERE node_id=? AND role='attention'""",
        (decision["node_id"],),
    ).fetchall()
    assert len(link) == 1
    assert link[0]["artifact_id"] == _artifact(db, decision["id"])["id"]


@pytest.mark.parametrize(
    "field", ["title", "context", "tradeoff", "rationale"],
)
def test_missing_required_text_is_rejected_before_any_write(db, field):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(service.PlanError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(**{field: "   "}),
        )
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "t" * 121),
        ("context", "c" * 1201),
        ("tradeoff", "t" * 801),
        ("rationale", "r" * 801),
    ],
)
def test_overlong_fields_are_rejected_at_the_declared_bound(db, field, value):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(service.PlanError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(**{field: value}),
        )


@pytest.mark.parametrize(
    "options",
    [
        [],
        [{"id": "A", "label": "Only", "detail": ""}],
        [{"id": chr(65 + index), "label": f"Option {index}", "detail": ""}
         for index in range(5)],
        [{"id": "B", "label": "Wrong start", "detail": ""},
         {"id": "C", "label": "Skipped A", "detail": ""}],
        [{"id": "B", "label": "Out of order", "detail": ""},
         {"id": "A", "label": "Second", "detail": ""}],
        [{"id": "A", "label": "Same", "detail": ""},
         {"id": "B", "label": "Same", "detail": ""}],
        [{"id": "A", "label": "", "detail": ""},
         {"id": "B", "label": "Fine", "detail": ""}],
        [{"id": "A", "label": "l" * 81, "detail": ""},
         {"id": "B", "label": "Fine", "detail": ""}],
        [{"id": "A", "label": "Fine", "detail": "d" * 241},
         {"id": "B", "label": "Also", "detail": ""}],
        "not-a-list",
    ],
)
def test_option_set_must_be_a_bounded_ordered_unique_abcd_prefix(db, options):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(service.PlanError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(options=options),
        )
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("recommendation", ["C", "", "Reuse the identity", None, 1])
def test_recommendation_must_name_one_supplied_option(db, recommendation):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(service.PlanError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(recommendation=recommendation),
        )


def test_other_enabled_is_required_to_be_true_so_the_user_is_never_stranded(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(service.PlanError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(other_enabled=False),
        )


def test_source_node_must_belong_to_the_authorized_plan(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    other = service.ensure_plan("thread", "cousin", db=db)
    elsewhere = _task(other.id, "Elsewhere", db=db)
    db.commit()

    with pytest.raises(PermissionError) as excinfo:
        decisions.draft(
            plan.id, source_node_id=elsewhere.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(),
        )
    assert elsewhere.id not in str(excinfo.value)


def test_an_unstructured_hand_registered_identity_never_enters_projection(db):
    from plans import artifact_links, decision_projection
    from registry import attention, models

    plan, source = _delegated(db)
    from infra.database import write_transaction
    with write_transaction(db, scope="test_attention_setup"):
        models.register_artifact(
            integration="local", type="plan-decision", external_id="hand-made",
            parent_thread_id="worker", metadata={"plan_decision": {"state": "raised"}},
            db=db,
        )
        artifact = _artifact(db, "hand-made")
        artifact_links.link(
            source.id, artifact["id"], "attention", actor_id="worker",
            provenance="hand", db=db,
        )
        attention.raise_attention(artifact["id"], db=db)
    db.commit()

    assert decision_projection.raised_decisions([plan.id], db=db) == []


# --- B. Lifecycle and authorization -----------------------------------------


def test_only_a_parent_reviewed_decision_may_raise_attention(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)

    with pytest.raises(service.PlanError):
        decisions.raise_to_user(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
        )
    assert _attention_state(db, decision["id"]) is None


@pytest.mark.parametrize(
    ("operation", "from_state"),
    [
        ("parent_review", "parent_reviewed"),
        ("parent_review", "raised"),
        ("raise_to_user", "raised"),
        ("parent_review", "answered"),
        ("raise_to_user", "answered"),
        ("update", "answered"),
        ("reorder", "answered"),
        ("invalidate", "answered"),
        ("parent_review", "invalidated"),
        ("raise_to_user", "invalidated"),
        ("update", "invalidated"),
        ("invalidate", "invalidated"),
    ],
)
def test_forbidden_transitions_are_rejected(db, operation, from_state):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _advance_to(db, plan, source, from_state)
    call = getattr(decisions, operation)
    kwargs = {"expected_revision": decision["revision"], "mutation_id": _mid(),
              "actor": _thread_actor("owner"), "provenance": "p", "db": db}
    if operation == "update":
        kwargs["title"] = "Rewritten"
    if operation == "reorder":
        kwargs["order_key"] = 3

    with pytest.raises(service.PlanError):
        call(decision["id"], **kwargs)


def _advance_to(db, plan, source, state: str):
    import plans.decisions as decisions

    if state == "draft":
        return _draft(db, plan, source)
    if state == "parent_reviewed":
        return _reviewed(db, plan, source)
    decision = _raised(db, plan, source)
    if state == "raised":
        return decision
    if state == "invalidated":
        return decisions.invalidate(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
        ).result["decision"]
    if state == "answered":
        return decisions.answer(
            decision["id"], option_id="A", expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_human(), provenance="p", db=db,
        ).result["decision"]
    raise AssertionError(f"unsupported state {state}")


def test_a_delegated_child_cannot_self_review_its_own_decision(db):
    """The child authors in its own plan; only the parent that spawned it reviews."""
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)

    with pytest.raises(PermissionError):
        decisions.parent_review(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
        )
    assert _attention_state(db, decision["id"]) is None


def test_a_delegated_child_cannot_draft_a_decision_in_its_parents_plan(db):
    """Only a plan's owner authors its decisions; the child asks in its own plan."""
    import plans.decisions as decisions

    parent_plan = service.ensure_plan("thread", "owner", db=db)
    step = _task(parent_plan.id, "Delegated step", db=db)
    db.commit()

    with pytest.raises(PermissionError):
        decisions.draft(
            parent_plan.id, source_node_id=step.id, mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            **_draft_kwargs(),
        )
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 0


def test_a_sibling_thread_is_not_the_parent_tier(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)

    with pytest.raises(PermissionError):
        decisions.parent_review(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("cousin"), provenance="p", db=db,
        )


def test_root_work_with_no_parent_tier_may_self_review(db):
    import plans.decisions as decisions

    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Root work", db=db)
    db.commit()
    decision = _draft(db, plan, source, actor=_thread_actor("owner"))

    reviewed = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="p", db=db,
    ).result["decision"]

    assert reviewed["state"] == "parent_reviewed"


def test_a_non_root_owner_thread_still_needs_its_spawning_parent(db):
    import plans.decisions as decisions

    plan = service.ensure_plan("thread", "worker", db=db)
    source = _task(plan.id, "Child work", db=db)
    db.commit()
    decision = _draft(db, plan, source, actor=_thread_actor("worker"))

    with pytest.raises(PermissionError):
        decisions.parent_review(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
        )
    assert decisions.parent_review(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("owner"), provenance="p", db=db,
    ).result["decision"]["state"] == "parent_reviewed"


def test_humans_cannot_draft_or_review_decisions(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    with pytest.raises(PermissionError):
        decisions.draft(
            plan.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_human(), provenance="p", db=db, **_draft_kwargs(),
        )
    decision = _draft(db, plan, source)
    with pytest.raises(PermissionError):
        decisions.parent_review(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_human(), provenance="p", db=db,
        )


def test_public_scope_plans_cannot_carry_decisions(db):
    import plans.decisions as decisions

    scope = service.ensure_plan("project_scope", "project-1", db=db)
    source = _task(scope.id, "Scope work", db=db)
    db.commit()

    with pytest.raises(PermissionError):
        decisions.draft(
            scope.id, source_node_id=source.id, mutation_id=_mid(),
            actor=_thread_actor("owner"), provenance="p", db=db, **_draft_kwargs(),
        )


@pytest.mark.parametrize("case", ["missing", "foreign", "archived_thread"])
def test_unavailable_decisions_are_denied_identically_without_leaking_ids(db, case):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    live = _draft(db, plan, source)
    foreign_plan = service.ensure_plan("thread", "foreign", db=db)
    foreign_source = _task(foreign_plan.id, "Foreign", db=db)
    db.commit()
    foreign = _draft(
        db, foreign_plan, foreign_source,
        actor=PlanActor("foreign", "user-2", "project-2"),
    )
    archived_plan = service.ensure_plan("thread", "archived", db=db)
    db.commit()
    targets = {"missing": "no-such-decision", "foreign": foreign["id"],
               "archived_thread": live["id"]}
    if case == "archived_thread":
        db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='worker'")
        db.commit()
    assert archived_plan is not None

    with pytest.raises((PermissionError, service.PlanError)) as excinfo:
        decisions.parent_review(
            targets[case], expected_revision=1, mutation_id=_mid(),
            actor=_thread_actor("owner"), provenance="p", db=db,
        )
    assert targets[case] not in str(excinfo.value)


def test_optimistic_revision_conflict_is_rejected_and_changes_nothing(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _reviewed(db, plan, source)

    with pytest.raises(service.PlanError):
        decisions.raise_to_user(
            decision["id"], expected_revision=decision["revision"] - 1,
            mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
        )
    assert _attention_state(db, decision["id"]) is None
    assert decisions.read(decision["id"], db=db)["state"] == "parent_reviewed"


def test_replaying_one_mutation_id_returns_the_recorded_result_exactly_once(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)
    mutation_id = _mid()

    first = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_thread_actor("owner"), provenance="p", db=db,
    )
    second = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_thread_actor("owner"), provenance="p", db=db,
    )

    assert first.result == second.result
    assert first.messages != ()
    assert second.messages == ()
    assert decisions.read(decision["id"], db=db)["revision"] == first.result["decision"]["revision"]


def test_rebinding_a_used_mutation_id_to_another_request_is_rejected(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    first = _draft(db, plan, source)
    second = _draft(db, plan, source, title="A second decision")
    mutation_id = _mid()
    decisions.parent_review(
        first["id"], expected_revision=first["revision"], mutation_id=mutation_id,
        actor=_thread_actor("owner"), provenance="p", db=db,
    )

    with pytest.raises(service.PlanError):
        decisions.parent_review(
            second["id"], expected_revision=second["revision"],
            mutation_id=mutation_id, actor=_thread_actor("owner"),
            provenance="p", db=db,
        )


def test_updating_a_raised_decision_returns_it_to_draft_and_clears_attention(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _raised(db, plan, source)
    assert _attention_state(db, decision["id"]) == "raised"

    updated = decisions.update(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db,
        context="Rewritten context after new evidence.",
    ).result["decision"]

    assert updated["state"] == "draft"
    assert updated["context"] == "Rewritten context after new evidence."
    assert updated["revision"] == decision["revision"] + 1
    assert _attention_state(db, decision["id"]) == "cleared"
    assert service._node_row(db, decision["node_id"])["state"] == "active"


def test_update_requires_a_stored_overlong_rationale_to_be_shortened(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)
    metadata = json.loads(_artifact(db, decision["id"])["metadata"])
    metadata["plan_decision"]["rationale"] = "r" * 500
    db.execute(
        "UPDATE artifact_identities SET metadata=? WHERE external_id=?",
        (json.dumps(metadata), decision["id"]),
    )
    db.commit()

    with pytest.raises(service.PlanError, match="stored rationale must be shortened to 240"):
        decisions.update(
            decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
            actor=_thread_actor("worker"), provenance="p", db=db,
            title="A title-only update",
        )

    updated = decisions.update(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db,
        rationale="Short rationale.",
    ).result["decision"]
    assert updated["rationale"] == "Short rationale."


def test_reorder_changes_only_canonical_order_and_never_the_pile(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    first = _raised(db, plan, source)
    second = _raised(db, plan, source, title="Second decision")

    reordered = decisions.reorder(
        second["id"], order_key=0, expected_revision=second["revision"],
        mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
    ).result["decision"]

    assert reordered["order_key"] == 0
    assert reordered["state"] == "raised"
    assert _attention_state(db, first["id"]) == "raised"
    assert _attention_state(db, second["id"]) == "raised"
    assert service._node_row(db, second["node_id"])["state"] == "review_requested"


def test_invalidate_clears_the_same_attention_and_closes_the_review_node(db):
    import plans.closure as closure
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _raised(db, plan, source)

    invalidated = decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db,
    ).result["decision"]

    assert invalidated["state"] == "invalidated"
    assert _attention_state(db, decision["id"]) == "cleared"
    node = service._node_row(db, decision["node_id"])
    # A withdrawn question is not a failure: it closes, never red (#2589).
    assert node["state"] == "closed"
    assert closure.settled_reasons(db, [node]) == {node["id"]: "Decision withdrawn"}
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 1


def test_invalidating_a_draft_never_raised_closes_its_node(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _draft(db, plan, source)

    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db,
    )

    assert service._node_row(db, decision["node_id"])["state"] == "closed"


# --- C. One artifact, one attention identity --------------------------------


def test_repeated_raise_activates_exactly_one_attention_identity(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _reviewed(db, plan, source)
    mutation_id = _mid()
    for _ in range(3):
        decisions.raise_to_user(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=mutation_id, actor=_thread_actor("owner"),
            provenance="p", db=db,
        )

    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_artifact_links WHERE node_id=?",
        (decision["node_id"],),
    ).fetchone()[0] == 1
    assert _attention_state(db, decision["id"]) == "raised"


def test_concurrent_drafts_on_separate_connections_create_distinct_identities(tmp_path):
    import plans.decisions as decisions

    path = tmp_path / "decisions.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    setup = connect()
    setup.executescript(SCHEMA_SQL)
    _seed(setup)
    plan = service.ensure_plan("thread", "worker", db=setup)
    source = _task(plan.id, "Implement", db=setup)
    setup.commit()
    setup.close()

    start = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def run(index: int) -> None:
        connection = connect()
        try:
            start.wait(timeout=10)
            results.append(decisions.draft(
                plan.id, source_node_id=source.id, mutation_id=f"concurrent-{index}",
                actor=_thread_actor("worker"), provenance="p", db=connection,
                **_draft_kwargs(title=f"Decision {index}"),
            ).result["decision"])
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert len({item["id"] for item in results}) == 2
    verify = connect()
    try:
        assert verify.execute(
            "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
        ).fetchone()[0] == 2
        assert {item["order_key"] for item in results} == {0, 1}
    finally:
        verify.close()


def test_replayed_draft_never_creates_a_second_identity(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    mutation_id = _mid()
    kwargs = dict(
        source_node_id=source.id, mutation_id=mutation_id,
        actor=_thread_actor("worker"), provenance="p", db=db, **_draft_kwargs(),
    )

    first = decisions.draft(plan.id, **kwargs)
    second = decisions.draft(plan.id, **kwargs)

    assert first.result == second.result
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()[0] == 1


def test_projection_returns_raised_decisions_in_canonical_order_without_duplicates(db):
    from plans import decision_projection

    plan, source = _delegated(db)
    third = _raised(db, plan, source, title="Third")
    first = _raised(db, plan, source, title="First")
    second = _raised(db, plan, source, title="Second")
    _reviewed(db, plan, source, title="Not raised yet")
    import plans.decisions as decisions

    decisions.reorder(
        first["id"], order_key=0, expected_revision=first["revision"],
        mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
    )
    decisions.reorder(
        second["id"], order_key=1, expected_revision=second["revision"],
        mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
    )
    decisions.reorder(
        third["id"], order_key=2, expected_revision=third["revision"],
        mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
    )
    db.commit()

    projected = decision_projection.raised_decisions([plan.id, plan.id], db=db)

    assert [item["title"] for item in projected] == ["First", "Second", "Third"]
    assert len({item["id"] for item in projected}) == 3


def test_projection_excludes_answered_invalidated_and_unraised_decisions(db):
    from plans import decision_projection, decisions

    plan, source = _delegated(db)
    raised = _raised(db, plan, source, title="Still open")
    answered = _raised(db, plan, source, title="Answered")
    invalid = _raised(db, plan, source, title="Invalid")
    _draft(db, plan, source, title="Draft only")
    _reviewed(db, plan, source, title="Reviewed only")
    decisions.answer(
        answered["id"], option_id="A", expected_revision=answered["revision"],
        mutation_id=_mid(), actor=_human(), provenance="p", db=db,
    )
    decisions.invalidate(
        invalid["id"], expected_revision=invalid["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db,
    )
    db.commit()

    projected = decision_projection.raised_decisions([plan.id], db=db)

    assert [item["id"] for item in projected] == [raised["id"]]


# The generic/typed split is now payload-proven rather than a type-level
# assertion: see `test_attention_queue.py` and `test_attention_queue_api.py`.


# --- E. Invalidation nudge and freshness ------------------------------------


def test_every_successful_mutation_emits_one_content_free_scoped_nudge(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)

    deferred = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="p", db=db, **_draft_kwargs(),
    )

    assert len(deferred.messages) == 1
    user_id, message = deferred.messages[0]
    assert user_id == "user-1"
    assert message["type"] == "plans_changed"
    parent_plan_id = db.execute(
        "SELECT parent_plan_id FROM plans WHERE id=?", (plan.id,)
    ).fetchone()["parent_plan_id"]
    # The worker's plan, then the plan that attaches it: its row embeds this one.
    assert [change["plan_id"] for change in message["plans"]] == [plan.id, parent_plan_id]
    assert message["plans"][0]["owner"] == {"kind": "thread", "id": "worker"}
    assert message["plans"][0]["revision"] == db.execute(
        "SELECT revision FROM plans WHERE id=?", (plan.id,)
    ).fetchone()["revision"]
    encoded = json.dumps(message)
    for secret in ("Where should decision state live?", "P5 needs one canonical",
                   "Reuse the identity"):
        assert secret not in encoded


def test_a_rolled_back_mutation_leaves_no_state_and_sends_no_nudge(db, monkeypatch):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _reviewed(db, plan, source)
    before = decisions.read(decision["id"], db=db)

    from registry import attention as registry_attention

    def explode(*_args, **_kwargs):
        raise RuntimeError("injected failure after the attention write")

    monkeypatch.setattr(registry_attention, "raise_attention", explode)

    with pytest.raises(RuntimeError):
        decisions.raise_to_user(
            decision["id"], expected_revision=decision["revision"],
            mutation_id=_mid(), actor=_thread_actor("owner"), provenance="p", db=db,
        )

    assert decisions.read(decision["id"], db=db) == before
    assert _attention_state(db, decision["id"]) is None
    assert service._node_row(db, decision["node_id"])["state"] == "pending"


def test_a_no_op_replay_reports_no_change_and_sends_no_nudge(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _reviewed(db, plan, source)
    mutation_id = _mid()
    decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_thread_actor("owner"), provenance="p", db=db,
    )

    replay = decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"],
        mutation_id=mutation_id, actor=_thread_actor("owner"), provenance="p", db=db,
    )

    assert replay.messages == ()


def test_plan_revision_advances_for_a_metadata_only_reorder(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _raised(db, plan, source)
    before = db.execute(
        "SELECT revision FROM plans WHERE id=?", (plan.id,)
    ).fetchone()["revision"]

    decisions.reorder(
        decision["id"], order_key=5, expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_thread_actor("worker"), provenance="p", db=db,
    )

    after = db.execute(
        "SELECT revision FROM plans WHERE id=?", (plan.id,)
    ).fetchone()["revision"]
    assert after > before


def test_every_decision_mutation_records_an_append_only_audit_event(db):
    import plans.decisions as decisions

    plan, source = _delegated(db)
    decision = _raised(db, plan, source)
    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor("worker"), provenance="tool.plans.decision", db=db,
    )
    db.commit()

    events = [
        row["event"] for row in db.execute(
            "SELECT event FROM plan_node_events WHERE node_id=? ORDER BY created_at,rowid",
            (decision["node_id"],),
        ).fetchall()
    ]
    for expected in ("decision_drafted", "decision_parent_reviewed",
                     "decision_raised", "decision_invalidated"):
        assert expected in events
