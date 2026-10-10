"""Explicit input cause, generated invariants and GitHub date-time policy."""
from __future__ import annotations

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from events.connectors import github_admission as admission
from events.connectors import github_outcome_input as shape
from events.connectors import github_outcomes as outcomes
from infra import drain, outcome_events
from infra.outcome_event_attributes import MAX_ATTRIBUTES_BYTES
from infra.outcome_event_input import OutcomeInputRefusal
from test_github_payload_refusal_http import send
from test_github_raw_fact_fault_classification import _FaultyWriter, _rows
from test_github_raw_facts_at_admission import _counters, _deliveries, _facts, admit, delivery
from webhook_ingress_fixtures import WEBHOOK_SECRET, _isolated_db  # noqa: F401


@pytest.fixture(autouse=True)
def own_admission(monkeypatch):
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    monkeypatch.setattr("api.webhooks._get_webhook_secret", lambda: WEBHOOK_SECRET)


@pytest.mark.parametrize("guard", ["identity", "budget"])
def test_deliberate_store_guard_after_real_first_write_retains_counted_receipt(
    main_db, monkeypatch, caplog, guard,
):
    event_type, payload = delivery("pr_opened")
    good = outcomes.map_github_outcomes(event_type, payload)
    invalid = outcomes.OutcomeDraft(
        dedupe_key="password" + "=" + "synthetic-value" if guard == "identity" else "budget-key",
        event_name="github.workflow_run.completed", ts=good[0].ts,
        subject_key=good[0].subject_key,
        attributes={"attempt": 10 ** MAX_ATTRIBUTES_BYTES} if guard == "budget" else {},
    )
    real = outcome_events.record_outcome_event
    written = []

    def track(db, **fields):
        result = real(db, **fields)
        written.append(fields["dedupe_key"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "map_github_outcomes", lambda *_: [*good, invalid])
        patch.setattr(outcome_events, "record_outcome_event", track)
        with caplog.at_level(logging.WARNING, logger=admission.log.name):
            record, _, _ = admit(event_type, payload, "d-explicit-refusal")
    assert written == [good[0].dedupe_key], "actual guard refused only after a real first insert"
    assert record.created
    assert _facts(main_db) == {}, "the complete derived set rolls back to the savepoint"
    assert _deliveries(main_db) == {"d-explicit-refusal": "received"}
    assert _counters(main_db) == {("mapping_failed", event_type): 1}
    messages = [record.getMessage() for record in caplog.records]
    warning = next(message for message in messages if "facts refused" in message)
    assert warning.endswith("OutcomeInputRefusal") and "d-explicit-refusal" in warning
    assert invalid.dedupe_key not in warning and payload["repository"]["full_name"] not in warning
    duplicate, _, _ = admit(event_type, payload, "d-explicit-refusal")
    assert not duplicate.created and _facts(main_db) == {}
    assert _counters(main_db) == {("mapping_failed", event_type): 1}


@pytest.mark.parametrize("fault", [ValueError, sqlite3.IntegrityError, OutcomeInputRefusal])
def test_refusal_counter_failure_after_first_write_aborts_the_outer_transaction(
    main_db, monkeypatch, fault,
):
    event_type, payload = delivery("pr_opened")
    good = outcomes.map_github_outcomes(event_type, payload)
    refused = outcomes.OutcomeDraft(
        "password" + "=" + "synthetic-value", good[0].event_name, good[0].ts, good[0].subject_key,
    )
    real = outcome_events.record_outcome_event
    written = []
    counted = []

    def track(db, **fields):
        result = real(db, **fields)
        written.append(fields["dedupe_key"])
        return result

    def fail_count(db, counter, event_name):
        counted.append((counter, event_name))
        raise fault("counter fault")

    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "map_github_outcomes", lambda *_: [*good, refused])
        patch.setattr(outcome_events, "record_outcome_event", track)
        patch.setattr(outcome_events, "count_outcome_event", fail_count)
        with pytest.raises(fault, match="counter fault"):
            admit(event_type, payload, "d-refusal-counter-fault")
    assert written == [good[0].dedupe_key] and counted == [("mapping_failed", event_type)]
    assert _facts(main_db) == {} and _deliveries(main_db) == {} and _counters(main_db) == {}
    record, _, _ = admit(event_type, payload, "d-refusal-counter-fault")
    assert record.created and set(_facts(main_db)) == {draft.dedupe_key for draft in good}
    duplicate, _, _ = admit(event_type, payload, "d-refusal-counter-fault")
    assert not duplicate.created and _counters(main_db) == {}


@pytest.mark.parametrize("field", ["source", "provenance", "attribution"])
def test_generated_store_metadata_constraint_fault_aborts_after_real_first_insert(
    main_db, monkeypatch, field,
):
    event_type, payload = delivery("pr_introduced_by_merged")
    expected = {draft.dedupe_key for draft in outcomes.map_github_outcomes(event_type, payload)}
    real = outcome_events.record_outcome_event
    written = []

    def corrupt_second(db, **fields):
        if written:
            fields[field] = "invalid-generated-value"
        result = real(db, **fields)
        written.append(fields["dedupe_key"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(outcome_events, "record_outcome_event", corrupt_second)
        with pytest.raises(sqlite3.IntegrityError):
            admit(event_type, payload, "d-generated-metadata")
    assert len(written) == 1
    assert _facts(main_db) == {} and _deliveries(main_db) == {} and _counters(main_db) == {}
    record, _, _ = admit(event_type, payload, "d-generated-metadata")
    assert record.created and set(_facts(main_db)) == expected


def test_invalid_timestamp_generated_after_valid_mapping_is_a_store_fault(main_db, monkeypatch):
    event_type, payload = delivery("pr_opened")
    good = outcomes.map_github_outcomes(event_type, payload)
    bad = outcomes.OutcomeDraft("bad-ts-key", good[0].event_name, "not-a-time", good[0].subject_key)
    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "map_github_outcomes", lambda *_: [*good, bad])
        with pytest.raises(ValueError) as raised:
            admit(event_type, payload, "d-generated-ts")
    assert type(raised.value) is ValueError, "the shared normalizer is not an input-refusal classifier"
    assert _facts(main_db) == {} and _deliveries(main_db) == {} and _counters(main_db) == {}
    record, _, _ = admit(event_type, payload, "d-generated-ts")
    assert record.created and set(_facts(main_db)) == {draft.dedupe_key for draft in good}


@pytest.mark.parametrize("fault", [ValueError, sqlite3.IntegrityError])
def test_http_store_fault_rolls_back_and_recovers_through_real_scheduler(
    _isolated_db, monkeypatch, fault,
):
    from main import app

    event_type, payload = delivery("pr_retargeted")
    scheduled = []
    real_schedule = admission._schedule

    def track_schedule(*args):
        scheduled.append(args[2])
        return real_schedule(*args)

    monkeypatch.setattr(admission, "_schedule", track_schedule)
    client = TestClient(app, raise_server_exceptions=False)
    writer = _FaultyWriter(fault("private fault marker"))
    with monkeypatch.context() as patch:
        patch.setattr(outcome_events, "record_outcome_event", writer)
        failed = send(client, event_type, payload, "d-http-store-fault")
    assert writer.calls, "the actual store inserted the first fact before the injected fault"
    assert failed.status_code == 500 and scheduled == []
    assert _rows(_isolated_db) == {"deliveries": [], "facts": [], "counters": []}
    assert drain._turn_admissions == 0
    repaired = send(client, event_type, payload, "d-http-store-fault")
    assert repaired.status_code == 200 and repaired.json()["status"] == "ignored"
    assert scheduled == ["d-http-store-fault"]
    rows = _rows(_isolated_db)
    assert rows["deliveries"] == ["d-http-store-fault"] and len(rows["facts"]) == 1
    assert rows["counters"] == [] and drain._turn_admissions == 0
    duplicate = send(client, event_type, payload, "d-http-store-fault")
    assert duplicate.json()["status"] == "duplicate" and scheduled == ["d-http-store-fault"]
    assert _rows(_isolated_db) == rows and drain._turn_admissions == 0


INVALID_TIMESTAMPS = (
    "2026-10-02", "2026-10-02T19:00:00", "20261002T190000Z",
    "2026-W40-5T19:00:00Z", "2026-10-02 19:00:00Z", "2026-10-02X19:00:00Z",
    "2026-10-02T19:00Z", "2026-10-02T19:00:00+00:00:30", "2026-10-02T19:00:00,5Z",
    "2026-10-02T19:00:00+00:60", "2026-02-30T19:00:00Z",
)


@pytest.mark.parametrize("value", INVALID_TIMESTAMPS)
def test_iso_parseability_does_not_establish_github_date_time(value):
    with pytest.raises(shape.PayloadRefusal):
        shape.timestamp(value)


@pytest.mark.parametrize("value,expected", [
    ("2026-10-02T19:00:00Z", "2026-10-02T19:00:00+00:00"),
    ("2026-10-02T19:00:00+00:00", "2026-10-02T19:00:00+00:00"),
    ("2026-10-02T12:00:00-07:00", "2026-10-02T19:00:00+00:00"),
    ("2026-10-02T19:00:00.123456Z", "2026-10-02T19:00:00.123456+00:00"),
    ("2026-10-02T21:30:00.5+02:30", "2026-10-02T19:00:00.500000+00:00"),
])
def test_full_zoned_date_time_preserves_source_text_and_normalized_instant(value, expected):
    assert shape.timestamp(value) == value
    assert outcome_events._utc(value) == expected


@pytest.mark.parametrize("fixture,obj,field", [
    ("pr_opened", "pull_request", "created_at"),
    ("pr_synchronize", "pull_request", "updated_at"),
    ("pr_reopened", "pull_request", "updated_at"),
    ("pr_closed_unmerged", "pull_request", "closed_at"),
    ("pr_closes_merged", "pull_request", "merged_at"),
    ("pr_retargeted", "pull_request", "updated_at"),
    ("workflow_run_success", "workflow_run", "updated_at"),
    ("check_suite_success", "check_suite", "updated_at"),
    ("issue_opened", "issue", "created_at"),
    ("issue_closed_completed", "issue", "closed_at"),
])
@pytest.mark.parametrize("value", ["2026-10-02", "2026-10-02T19:00:00"])
def test_each_consumed_timestamp_refuses_before_drafting_and_counts_once(
    main_db, fixture, obj, field, value,
):
    event_type, payload = delivery(fixture)
    payload[obj][field] = value
    with pytest.raises(shape.PayloadRefusal):
        outcomes.map_github_outcomes(event_type, payload)
    record, _, _ = admit(event_type, payload, "d-source-time")
    assert record.created and _facts(main_db) == {}
    assert _deliveries(main_db) == {"d-source-time": "received"}
    assert _counters(main_db) == {("mapping_failed", event_type): 1}
    duplicate, _, _ = admit(event_type, payload, "d-source-time")
    assert not duplicate.created and _counters(main_db) == {("mapping_failed", event_type): 1}


@pytest.mark.parametrize("value", ["2026-10-02", "2026-10-02T19:00:00"])
def test_http_bad_source_time_is_refused_before_real_edited_scheduler(_isolated_db, value):
    from main import app

    event_type, payload = delivery("pr_retargeted")
    payload["pull_request"]["updated_at"] = value
    client = TestClient(app, raise_server_exceptions=False)
    response = send(client, event_type, payload, "d-http-source-time")
    assert response.status_code == 200 and response.json()["status"] == "ignored"
    expected = {"deliveries": ["d-http-source-time"], "facts": [], "counters": ["mapping_failed"]}
    assert _rows(_isolated_db) == expected and drain._turn_admissions == 0
    duplicate = send(client, event_type, payload, "d-http-source-time")
    assert duplicate.json()["status"] == "duplicate" and _rows(_isolated_db) == expected


def test_unused_timestamp_stays_unconsumed_and_common_naive_utc_policy_is_unchanged():
    event_type, payload = delivery("pr_opened")
    payload["action"] = "labeled"
    payload["pull_request"]["created_at"] = "not-a-time"
    assert outcomes.map_github_outcomes(event_type, payload) == []
    assert outcome_events._utc("2026-10-02T19:00:00") == "2026-10-02T19:00:00+00:00"
