"""Served correction counts include comment facts and legacy facts, and exclude decision answers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from doc_feedback_helpers import seed_document
from tests.test_correction_notes import _version  # noqa: F401
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    UNREADABLE,
    _facts,
    _record,
    _seed_decision,
    _user_comment,
    db,
)


def test_measurement_excludes_decision_answers_from_correction_rates(db):
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import unreadable_facts

    now = datetime.now(timezone.utc).isoformat()
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _seed_decision(db)
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('m','owner','user','Wrong.','{}',?)",
        (now,),
    )
    db.commit()
    from jobs.corrections_recording import _record_fact, candidate_for_message

    assert _record_fact(candidate_for_message("m"), CORRECTION, _version())
    _record(db, "doc_comment", comment)
    _record(db, "plan_decision_answer", "dec")
    _record(db, "plan_decision_answer", "dec", payload=UNREADABLE, version=_version("v2"))

    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1) == 2
    # Like for like across versions: the chat message alone.
    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1, messages_only=True) == 1
    assert unreadable_facts(db, _version("v2")) == 0
    assert any(subject == "plan-decision-answer:dec:7" for subject, _ in _facts(db))  # triage still sees it


def test_a_legacy_fact_with_message_id_and_no_source_kind_still_counts(db):
    from core.corrections_recording import version_attributes
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import unreadable_facts

    now = datetime.now(timezone.utc).isoformat()
    for message_id, label in (("old-correction", "correction"), ("old-unreadable", "unreadable")):
        attributes = {
            "message_id": message_id, "thread_id": "owner", "label": label, **version_attributes(_version()),
            "truncated": "false", "original_tokens": 3, "failure_reason": "none" if label == "correction" else "timeout",
        }
        db.execute(
            """INSERT INTO outcome_events
               (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"legacy-{message_id}", f"legacy:{message_id}", "measurement.correction", "corsia", "agent_recorded",
             now, now, f"message:{message_id}", json.dumps(attributes)),
        )
    db.commit()

    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1) == 1
    assert unreadable_facts(db, _version()) == 1
