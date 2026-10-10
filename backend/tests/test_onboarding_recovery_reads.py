"""The read-only Plans evidence used by first-run recovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from plans import decisions, public, service
from plans.boundary import PlanActor
from tests.test_plan_decisions import _draft_kwargs, _mid, _task, db  # noqa: F401
from tests.plan_writer_authorizer import raw_plan_seed


def _author(owner_thread_id: str = "owner") -> PlanActor:
    if owner_thread_id == "foreign":
        return PlanActor(owner_thread_id, "user-2", "project-2")
    return PlanActor(owner_thread_id, "user-1", "project-1")


def _raised_decision(db, owner_thread_id: str = "owner"):
    plan = service.ensure_plan("thread", owner_thread_id, db=db)
    source = _task(plan.id, "Choose first-run consent", db=db)
    db.commit()
    decision = decisions.draft(
        plan.id,
        source_node_id=source.id,
        mutation_id=_mid(),
        actor=_author(owner_thread_id),
        provenance="test.onboarding_recovery",
        db=db,
        **_draft_kwargs(),
    ).result["decision"]
    for step in (decisions.parent_review, decisions.raise_to_user):
        decision = step(
            decision["id"],
            expected_revision=decision["revision"],
            mutation_id=_mid(),
            actor=_author(owner_thread_id),
            provenance="test.onboarding_recovery",
            db=db,
        ).result["decision"]
    db.commit()
    return decision


def _human() -> PlanActor:
    return PlanActor(None, "user-1", None, kind="user")


def _boundary() -> datetime:
    return datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_raised_decision_is_historical_contact_after_withdrawal(db):
    decision = _raised_decision(db)

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is True

    decisions.invalidate(
        decision["id"],
        expected_revision=decision["revision"],
        mutation_id=_mid(),
        actor=_author(),
        provenance="test.onboarding_recovery",
        db=db,
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is True


def test_raised_decision_is_historical_contact_after_answer(db):
    decision = _raised_decision(db)

    decisions.answer(
        decision["id"],
        option_id="A",
        expected_revision=decision["revision"],
        mutation_id=_mid(),
        actor=_human(),
        provenance="test.onboarding_recovery",
        db=db,
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is True


def test_retired_source_does_not_erase_historical_contact(db):
    decision = _raised_decision(db)
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET archived_at='retired' WHERE id=?",
            (decision["source_node_id"],),
        )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is True


def test_draft_or_parent_reviewed_decision_is_not_contact(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Consent", db=db)
    db.commit()
    decision = decisions.draft(
        plan.id,
        source_node_id=source.id,
        mutation_id=_mid(),
        actor=_author(),
        provenance="test.onboarding_recovery",
        db=db,
        **_draft_kwargs(),
    ).result["decision"]
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False

    decisions.parent_review(
        decision["id"],
        expected_revision=decision["revision"],
        mutation_id=_mid(),
        actor=_author(),
        provenance="test.onboarding_recovery",
        db=db,
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False


def test_foreign_plan_decision_is_not_contact(db):
    _raised_decision(db, "foreign")

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False


def test_generic_decision_raised_event_is_not_contact(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Generic event", db=db)
    db.execute(
        """INSERT INTO plan_node_events
           (id, node_id, event, provenance, payload_json, created_at)
           VALUES ('generic-event', ?, 'decision_raised', 'test', '{}', '2026-10-03T08:00:00Z')""",
        (source.id,),
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False


def test_malformed_decision_metadata_is_not_contact(db):
    decision = _raised_decision(db)
    db.execute(
        "UPDATE artifact_identities SET metadata='{}' WHERE external_id=?",
        (decision["id"],),
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False


def test_wrong_node_in_the_owner_plan_is_not_contact(db):
    decision = _raised_decision(db)
    row = db.execute(
        "SELECT id, metadata FROM artifact_identities "
        "WHERE integration='local' AND type='plan-decision' AND external_id=?",
        (decision["id"],),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    metadata["plan_decision"]["node_id"] = decision["source_node_id"]
    db.execute(
        "UPDATE artifact_identities SET metadata=? WHERE id=?",
        (json.dumps(metadata), row["id"]),
    )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False


@pytest.mark.parametrize(
    ("timestamps", "expected"),
    [
        (("2026-10-03T00:59:59-07:00",), False),
        (("2026-10-03T01:00:00-07:00",), True),
        (("2026-10-03T08:00:00Z",), True),
        (("2026-10-03 08:00:00",), True),
        ((None, "malformed", "2026-10-03T00:59:59-07:00", "2026-10-03 08:00:00"), True),
    ],
)
def test_historical_raised_compares_parsed_instants(timestamps, expected):
    from plans import onboarding_reads

    body = {
        "state": "raised", "order_key": 0, "revision": 1, "title": "Consent",
        "context": "Context", "tradeoff": "Tradeoff", "recommendation": "A",
        "rationale": "Rationale", "options": [], "other_enabled": True,
        "plan_id": "plan", "node_id": "node", "source_node_id": "source",
        "thread_id": "owner",
    }
    rows = [
        {"created_at": timestamp, "metadata": json.dumps({"plan_decision": body}),
         "plan_id": "plan", "node_id": "node", "owner_id": "owner"}
        for timestamp in timestamps
    ]

    class CandidateRows:
        def execute(self, _sql, _params):
            return rows

    assert onboarding_reads.has_genuine_decision_raised_at_or_after(
        CandidateRows(), owner_thread_id="owner", boundary=datetime(2026, 10, 3, 8),
    ) is expected


def _prohibited_scans(details: list[str]) -> list[str]:
    aliases = {
        "e": "plan_node_events",
        "l": "plan_node_artifact_links",
        "i": "artifact_identities",
    }
    prohibited = set(aliases.values())
    return [
        detail for detail in details
        if len(parts := detail.lower().split()) >= 2
        and parts[0] == "scan"
        and aliases.get(parts[1], parts[1]) in prohibited
    ]


def test_query_plan_is_pinned_and_has_no_prohibited_alias_or_table_scan(db):
    from plans import onboarding_reads

    details = [
        row[3].lower()
        for row in db.execute(
            f"EXPLAIN QUERY PLAN {onboarding_reads._HISTORICAL_RAISED_SQL}", ("owner",),
        )
    ]
    assert details == [
        "search p using index sqlite_autoindex_plans_2 (owner_kind=? and owner_id=?)",
        "search n using index idx_plan_nodes_tree (plan_id=?)",
        "search l using primary key (node_id=?)",
        "search e using index idx_plan_node_events_node (node_id=?)",
        "search i using index sqlite_autoindex_artifact_identities_1 (id=?)",
        "use temp b-tree for order by",
    ]
    assert _prohibited_scans(details) == []
    assert _prohibited_scans([
        "scan e", "scan plan_node_artifact_links", "scan i", "scan artifact_identities",
    ]) == ["scan e", "scan plan_node_artifact_links", "scan i", "scan artifact_identities"]


def test_large_negative_owner_plan_has_no_historical_contact(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    with raw_plan_seed(db):
        db.executemany(
            """INSERT INTO plan_nodes
               (id, plan_id, parent_id, order_key, kind, title, acceptance_criteria,
                state, commitment_id, revision, created_at, updated_at)
               VALUES (?, ?, NULL, ?, 'task', ?, 'DOD', 'pending', ?, 1, 't', 't')""",
            [
                (f"unrelated-{index}", plan.id, index, f"Unrelated task {index}",
                 f"unrelated-{index}")
                for index in range(1_000)
            ],
        )
    db.commit()

    assert public.has_genuine_decision_raised_at_or_after(
        db, owner_thread_id="owner", boundary=_boundary(),
    ) is False
