"""`decision_projection.answerable` reads one row per decision, with the old result.

The reference below is the statement as it shipped before it joined each decision's
own source node: every in-work node of the plan, once per decision, with
membership tested in Python. The new statement must return exactly what it returns.
"""
from __future__ import annotations

import json

import pytest

import plans.service as service
from plans import activity_projection, decision_projection, node_states
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_activity_decision_facts import _body
from tests.test_plan_activity_index import _actor, _task, db  # noqa: F401


def _reference_answerable(db, source_nodes: dict[str, str]) -> dict[str, bool]:
    if not source_nodes:
        return {}
    marks = ",".join("?" for _ in source_nodes)
    rows = db.execute(
        f"""SELECT l.artifact_id AS artifact_id, src.id AS source_node_id
              FROM plan_node_artifact_links l
              JOIN plan_nodes n ON n.id = l.node_id AND n.archived_at IS NULL
                                AND n.state = 'review_requested'
              JOIN plans p ON p.id = n.plan_id AND p.state != 'archived'
              JOIN threads s ON s.id = p.owner_id
              JOIN plan_nodes src
                ON src.plan_id = p.id AND {node_states.in_work_sql('src')}
               AND {node_states.outside_retired_branch_sql('src')}
             WHERE l.role = 'attention'
               AND p.owner_kind = 'thread'
               AND s.archived_at IS NULL AND s.purged_at IS NULL
               AND COALESCE(s.lifecycle_state,'active') != 'archived'
               AND l.artifact_id IN ({marks})""",
        tuple(sorted(source_nodes)),
    ).fetchall()
    live = {(row["artifact_id"], row["source_node_id"]) for row in rows}
    return {
        artifact_id: (artifact_id, source_node_id) in live
        for artifact_id, source_node_id in source_nodes.items()
    }


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


def test_answerable_matches_the_reference_statement_on_every_source_situation(db):
    batch = _seed_decisions(db)
    expected = _reference_answerable(db, batch)

    assert decision_projection.answerable(db, batch) == expected
    assert expected["live"] is True and expected["accepted"] is True
    assert expected["failed"] is True
    assert not any(expected[name] for name in (
        "closed", "archived", "retired", "other_plan", "absent",
        "review_active", "review_closed", "archived_owner"))
    # A decision naming a live node of its own plan other than the one it was drafted
    # from is a different identity: pairing is by the named node, as before.
    other_node = next(source for name, source in batch.items() if name == "accepted")
    swapped = {"live": other_node, "accepted": batch["live"]}
    assert decision_projection.answerable(db, swapped) == _reference_answerable(db, swapped)
    assert decision_projection.answerable(db, {}) == {}


def test_workspace_activity_read_keeps_its_revision_token(db, monkeypatch):
    _seed_decisions(db)
    after = activity_projection.read_workspace(actor=_actor(), db=db)
    monkeypatch.setattr(decision_projection, "answerable", _reference_answerable)
    before = activity_projection.read_workspace(actor=_actor(), db=db)

    assert before["revision_token"] == after["revision_token"]
    assert {k: v for k, v in before.items() if k != "as_of"} == {
        k: v for k, v in after.items() if k != "as_of"}
    assert after["attention"]["awaiting_count"] > 0


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
