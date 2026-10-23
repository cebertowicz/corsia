"""Served correction counts: the weekly figure reads chat messages, unreadable counts add comments."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from doc_feedback_helpers import seed_document
from tests.test_correction_notes import _version
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    UNREADABLE,
    _facts,
    _record,
    _seed_decision,
    _user_comment,
    db,
)


def test_weekly_figure_counts_chat_messages_and_unreadable_counts_comments_not_answers(db):
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import _record_fact, candidate_for_message, unreadable_facts

    now = datetime.now(timezone.utc).isoformat()
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _seed_decision(db)
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('m','owner','user','Wrong.','{}',?)",
        (now,),
    )
    db.commit()

    assert _record_fact(candidate_for_message("m"), CORRECTION, _version())
    _record(db, "doc_comment", comment)
    _record(db, "plan_decision_answer", "dec")
    _record(db, "doc_comment", comment, payload=UNREADABLE, version=_version("v2"))
    _record(db, "plan_decision_answer", "dec", payload=UNREADABLE, version=_version("v2"))

    # Like for like across versions: the chat message alone, never the comment or the answer.
    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1) == 1
    assert unreadable_facts(db, _version("v2")) == 1  # the comment; the decision answer stays triage-only
    assert any(subject == "plan-decision-answer:dec:7" for subject, _ in _facts(db))  # triage still sees it


def test_labelled_sent_back_facts_do_not_change_classifier_version_metrics(db):
    from core.sent_back_corrections import SentBackCorrection, record_sent_back_correction
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import unreadable_facts

    now = datetime.now(timezone.utc).isoformat()
    weekly_before = weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1)
    unreadable_before = unreadable_facts(db, _version())

    assert record_sent_back_correction(
        SentBackCorrection("sent-back-event", now, None, "owner"), db=db,
    )
    attributes = json.loads(db.execute("SELECT attributes FROM outcome_events").fetchone()["attributes"])
    assert attributes["label"] == "correction"

    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1) == weekly_before
    assert unreadable_facts(db, _version()) == unreadable_before


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
