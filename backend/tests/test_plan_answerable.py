"""`decision_projection.answerable` settles each decision on the one source node it names.

A decision is answerable only while its attention link is live, its review node is
in `review_requested`, the named source node is in work and outside a retired branch
of the same plan, the thread plan is not archived and the owner thread is live. The
statement drives from the caller's batch and reaches each source node by primary key.
"""
from __future__ import annotations

import json

import pytest

import plans.service as service
from plans import decision_projection
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_activity_decision_facts import _body
from tests.test_plan_activity_index import _task, db  # noqa: F401


def _decision(db, plan_id: str, thread_id: str, name: str, *, source_id: str | None,
              review_state: str = "review_requested") -> str:
    review = _task(plan_id, f"Review {name}", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (review_state, review.id))
    body = _body() | {"plan_id": plan_id, "node_id": review.id, "source_node_id": source_id,
                      "thread_id": thread_id, "order_key": 0, "revision": 1}
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES (?,'local','plan-decision',?,'active',?,'t','t')""",
        (name, name, json.dumps({"attention_state": "raised", "plan_decision": body})),
    )
    db.execute(
        """INSERT INTO plan_node_artifact_links
           (node_id,artifact_id,role,provenance,created_at)
           VALUES (?,?,'attention','test','t')""",
        (review.id, name),
    )
    return name


def _seed_decisions(db) -> dict[str, str]:
    """One plan with a decision per source-node situation; returns artifact -> source."""
    plan = service.ensure_plan("thread", "moving", db=db)
    sources = {
        label: _task(plan.id, f"Source {label}", db=db).id
        for label in ("live", "accepted", "failed", "closed", "archived", "retired", "sibling")
    }
    other = service.ensure_plan("thread", "blocked", db=db)
    foreign_source = _task(other.id, "Foreign source", db=db).id
    retired = service.add_node(
        plan.id, kind="phase", title="Retired", acceptance_criteria="done", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (sources["accepted"],))
        db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (sources["failed"],))
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (sources["closed"],))
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (sources["archived"],))
        db.execute("UPDATE plan_nodes SET parent_id=?,state='accepted' WHERE id=?",
                   (retired.id, sources["retired"]))
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (retired.id,))
    named = {
        "live": _decision(db, plan.id, "moving", "live", source_id=sources["live"]),
        "accepted": _decision(db, plan.id, "moving", "accepted", source_id=sources["accepted"]),
        "failed": _decision(db, plan.id, "moving", "failed", source_id=sources["failed"]),
        "closed": _decision(db, plan.id, "moving", "closed", source_id=sources["closed"]),
        "archived": _decision(db, plan.id, "moving", "archived", source_id=sources["archived"]),
        "retired": _decision(db, plan.id, "moving", "retired", source_id=sources["retired"]),
        "other_plan": _decision(db, plan.id, "moving", "other_plan", source_id=foreign_source),
        "absent": _decision(db, plan.id, "moving", "absent", source_id="absent"),
        "review_active": _decision(db, plan.id, "moving", "review_active",
                                   source_id=sources["live"], review_state="active"),
        "review_closed": _decision(db, plan.id, "moving", "review_closed",
                                   source_id=sources["live"], review_state="closed"),
    }
    # A decision on a plan whose owner thread is archived, one on an archived plan.
    archived_owner = service.ensure_plan("thread", "complete", db=db)
    named["archived_owner"] = _decision(
        db, archived_owner.id, "complete", "archived_owner",
        source_id=_task(archived_owner.id, "Source owner", db=db).id)
    db.execute("UPDATE threads SET archived_at='t' WHERE id='complete'")
    db.commit()
    return {name: _source_of(db, name) for name in named.values()}


def _source_of(db, artifact_id: str) -> str:
    row = db.execute("SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,)).fetchone()
    return json.loads(row["metadata"])["plan_decision"]["source_node_id"]


def test_answerable_on_every_source_situation(db):
    batch = _seed_decisions(db)

    assert decision_projection.answerable(db, batch) == {
        "live": True, "accepted": True, "failed": True,
        "closed": False, "archived": False, "retired": False,
        "other_plan": False, "absent": False,
        "review_active": False, "review_closed": False, "archived_owner": False,
    }
    # Pairing is by the node each decision names: swapping two live names changes nothing.
    swapped = {"live": batch["accepted"], "accepted": batch["live"]}
    assert decision_projection.answerable(db, swapped) == {"live": True, "accepted": True}
    assert decision_projection.answerable(db, {"live": batch["closed"]}) == {"live": False}
    assert decision_projection.answerable(db, {}) == {}


def test_answerable_drives_from_the_batch_and_joins_source_by_primary_key(db):
    batch = _seed_decisions(db)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    decision_projection.answerable(db, batch)
    db.set_trace_callback(None)
    expanded = next(text for text in statements if "plan_node_artifact_links" in text)
    plan = " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + expanded))

    assert plan.index("SCAN pair") < plan.index("SEARCH l ") < plan.index("SEARCH src ")
    assert "SEARCH src USING INDEX sqlite_autoindex_plan_nodes_1 (id=?)" in plan
    assert "idx_plan_nodes_tree" not in plan.split("SEARCH n ")[0] + plan.split("SEARCH src ")[1]
